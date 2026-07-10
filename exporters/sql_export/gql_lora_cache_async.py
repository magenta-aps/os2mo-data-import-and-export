import asyncio
import datetime
import logging
import os
import pickle
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from uuid import UUID

from fastramqpi.ra_utils.async_to_sync import async_to_sync
from fastramqpi.raclients.graph.client import GraphQLClient
from gql import gql
from gql.client import AsyncClientSession
from more_itertools import first
from tenacity import Retrying
from tenacity import stop_after_delay
from tenacity import wait_random_exponential

from .config import GqlLoraCacheSettings
from .config import get_gql_cache_settings

RETRY_MAX_TIME = 5 * 60


logger = logging.getLogger(__name__)


# used to correctly insert the object into the cache
def insert_obj(obj: dict, cache: dict) -> None:
    if obj is None:
        return
    if len(obj["obj"]) == 0:
        return
    if obj["uuid"] in cache:
        cache[obj["uuid"]].extend(obj["obj"])
    else:
        cache[obj["uuid"]] = obj["obj"]


# when getting a query using current, the object is a single dict. When getting a
# historic query it is a list of dicts. in order to uniformly process the two states
# we wrap the current object in a list
def align_current(item: dict) -> dict:
    item["obj"] = [item["obj"]]
    return item


# Does various transformations on a cache to align it with the old lora cache
def convert_dict(
    query_res: dict,
    resolve_object: bool = True,
    resolve_validity: bool = True,
    replace_dict: dict = {},
) -> dict:
    def replace(d: dict, dictionary: dict):
        for replace_from, replace_to in dictionary.items():
            d[replace_to] = d.pop(replace_from)
        return d

    def res_validity(d: dict):
        validity = d.pop("validity")
        if "from" in validity:
            if validity["from"]:
                d["from_date"] = str(
                    datetime.datetime.fromisoformat(validity["from"]).date()
                )
            else:
                d["from_date"] = str(datetime.datetime(1, 1, 1).date())
        if "to" in validity:
            if validity["to"]:
                d["to_date"] = str(
                    datetime.datetime.fromisoformat(validity["to"]).date()
                )
            else:
                d["to_date"] = "9999-12-31"
        return d

    uuid = query_res.pop("uuid")

    obj_list = []
    if resolve_object:
        for obj in query_res["obj"]:
            if obj is None:
                return {}
            if resolve_validity:
                obj = res_validity(obj)
            obj = replace(obj, replace_dict)
            obj_list.append(obj)

        return {"uuid": uuid, "obj": obj_list}

    if resolve_validity:
        query_res = res_validity(query_res)

    return {uuid: replace(query_res["obj"], replace_dict)}


class GQLLoraCache:
    def __init__(
        self,
        resolve_dar: bool = True,
        full_history: bool = False,
        skip_past: bool = False,
        settings=None,
    ):
        logger.info(
            f"Initialising LoRa cache, {resolve_dar=}, {full_history=}, {skip_past=}"
        )
        if isinstance(settings, dict):
            settings = None
        self.resolve_dar = resolve_dar
        self.settings: GqlLoraCacheSettings = settings or get_gql_cache_settings()
        self.page_size = self.settings.std_page_size

        self.full_history = full_history
        self.skip_past = skip_past

        self.facets: dict = {}
        self.classes: dict = {}
        self.users: dict = {}
        self.units: dict = {}
        self.addresses: dict = {}
        self.engagements: dict = {}
        self.managers: dict = {}
        self.associations: dict = {}
        self.leaves: dict = {}
        self.itsystems: dict = {}
        self.it_connections: dict = {}
        self.kles: dict = {}
        self.related: dict = {}
        self.dar_cache: dict = {}

        self._gql_client_session: AsyncClientSession | None = None

    def make_client(self) -> GraphQLClient:
        return GraphQLClient(
            url=f"{self.settings.mora_base}/graphql/v30",
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret,  # type: ignore
            auth_realm=self.settings.auth_realm,
            auth_server=self.settings.auth_server,  # type: ignore
            httpx_client_kwargs={"timeout": 1000},
            execute_timeout=1000,
        )

    async def gql_client_session(self) -> AsyncClientSession:
        if (session := self._gql_client_session) is not None:
            return session
        client = self.make_client()
        # NOTE: The client is never closed 👍
        session = self._gql_client_session = await client.__aenter__()
        return session

    async def _execute_query(
        self,
        query: str,
        variable_values: dict | None = None,
        do_paged: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        next_cursor = None
        session = await self.gql_client_session()
        while True:
            for attempt in Retrying(
                wait=wait_random_exponential(multiplier=2, max=30),
                stop=stop_after_delay(RETRY_MAX_TIME),
                reraise=True,
            ):
                with attempt:
                    if do_paged:
                        limit = self.page_size
                        cursor = next_cursor
                    else:
                        limit = None
                        cursor = None
                    result = await session.execute(
                        document=gql(query),
                        variable_values=dict(
                            limit=limit,
                            cursor=cursor,
                            **(variable_values or {}),
                        ),
                        get_execution_result=True,
                    )
            for obj in result.data["page"]["objects"]:  # type: ignore
                yield obj
            next_cursor = result.data["page"]["page_info"]["next_cursor"]  # type: ignore
            if next_cursor is None:
                break

    async def _get_org_uuid(self) -> str:
        root_org_query = gql(
            """
            query {
                org {
                    uuid
                }
            }
            """
        )
        session = await self.gql_client_session()
        res = await session.execute(root_org_query)
        return res["org"]["uuid"]

    async def _cache_lora_facets(self):
        obj = await self._fetch_facets()
        self.facets.update(obj)

    async def _fetch_facets(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching facets")
        query = """
            query ($limit: int, $cursor: Cursor) {
                page: facets(limit: $limit, cursor: $cursor) {
                    objects {
                        uuid
                        obj: current {
                            uuid
                            user_key
                        }
                    }
                    page_info {
                        next_cursor
                    }
                }
            }
        """
        variables = {
            "filter": {
                "uuids": str(uuid) if uuid is not None else None,
            },
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            obj = convert_dict(obj, resolve_object=False, resolve_validity=False)
            res.update(obj)
        return res

    async def _cache_lora_classes(self):
        obj = await self._fetch_classes()
        self.classes.update(obj)

    async def _fetch_classes(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching classes")
        query = """
            query ($limit: int, $cursor: Cursor) {
                page: classes(limit: $limit, cursor: $cursor) {
                    objects {
                        uuid
                        obj: current {
                            uuid
                            user_key
                            name
                            scope
                            facet_uuid
                        }
                    }
                    page_info {
                        next_cursor
                    }
                }
            }
        """
        variables = {
            "filter": {
                "uuids": str(uuid) if uuid is not None else None,
            },
        }
        dictionary = {"name": "title", "facet_uuid": "facet"}

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            obj = convert_dict(
                obj,
                resolve_object=False,
                resolve_validity=False,
                replace_dict=dictionary,
            )
            res.update(obj)
        return res

    async def _cache_lora_itsystems(self):
        obj = await self._fetch_itsystems()
        self.itsystems.update(obj)

    async def _fetch_itsystems(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching it systems")
        query = """
            query ($limit: int, $cursor: Cursor) {
                page: itsystems(limit: $limit, cursor: $cursor) {
                    objects {
                        uuid
                        obj: current {
                            uuid
                            user_key
                            name
                        }
                    }
                    page_info {
                        next_cursor
                    }
                }
            }
        """
        variables = {
            "filter": {
                "uuids": str(uuid) if uuid is not None else None,
            },
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            obj = convert_dict(obj, resolve_object=False, resolve_validity=False)
            res.update(obj)
        return res

    async def _cache_lora_users(self):
        obj = await self._fetch_users()
        self.users.update(obj)

    async def _fetch_users(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching users")
        if self.full_history:
            query = """
                query (
                    $filter: EmployeeFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: employees(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                cpr_no
                                user_key
                                name
                                givenname
                                surname
                                nickname
                                nickname_givenname
                                nickname_surname
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: EmployeeFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: employees(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                cpr_no
                                user_key
                                name
                                givenname
                                surname
                                nickname
                                nickname_givenname
                                nickname_surname
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "cpr_no": "cpr",
            "givenname": "fornavn",
            "surname": "efternavn",
            "name": "navn",
            "nickname": "kaldenavn",
            "nickname_givenname": "kaldenavn_fornavn",
            "nickname_surname": "kaldenavn_efternavn",
        }

        res: dict = {}

        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_units(self):
        obj = await self._fetch_units()
        self.units.update(obj)

    async def _fetch_units(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching org units")

        org_uuid = await self._get_org_uuid()

        async def format_managers_and_location(qr: dict):
            def find_manager(managers: list[dict]) -> str | None:
                if not managers:
                    return None
                if self.settings.primary_manager_responsibility is None:
                    return first(managers)["uuid"]
                return first(
                    map(
                        lambda m: m["uuid"],
                        filter(
                            lambda ma: (
                                self.settings.primary_manager_responsibility
                                in ma["responsibility_uuids"]
                            ),
                            managers,
                        ),
                    ),
                    None,
                )

            for man in qr["obj"]:
                if man is None:
                    continue
                if not self.full_history:
                    if man["manager_uuid"]:
                        man["manager_uuid"] = find_manager(man["manager_uuid"])
                    else:
                        man["manager_uuid"] = None

                    if man["acting_manager_uuid"]:
                        man["acting_manager_uuid"] = find_manager(
                            man["acting_manager_uuid"]
                        )
                    else:
                        man["acting_manager_uuid"] = None

                    ancestors = man.pop("ancestors")
                    location = man["name"]
                    for ancestor in ancestors:
                        location = ancestor["name"] + "\\" + location

                    man["location"] = location
            return qr

        if self.full_history:
            query = """
                query (
                    $filter: OrganisationUnitFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: org_units(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                user_key
                                name
                                unit_type_uuid
                                org_unit_level_uuid
                                time_planning_uuid
                                parent_uuid
                                org_unit_hierarchy_uuid: org_unit_hierarchy
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: OrganisationUnitFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: org_units(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                user_key
                                name
                                unit_type_uuid
                                org_unit_level_uuid
                                time_planning_uuid
                                parent_uuid
                                org_unit_hierarchy_uuid: org_unit_hierarchy
                                manager_uuid: managers(inherit: false) {
                                    org_unit_uuid
                                    responsibility_uuids
                                    uuid
                                }
                                acting_manager_uuid: managers(inherit: true) {
                                    org_unit_uuid
                                    responsibility_uuids
                                    uuid
                                }
                                ancestors {
                                    name
                                    uuid
                                }
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "org_unit_level_uuid": "level",
            "org_unit_hierarchy_uuid": "org_unit_hierarchy",
            "parent_uuid": "parent",
            "unit_type_uuid": "unit_type",
            "time_planning_uuid": "time_planning",
        }
        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            for item in obj["obj"]:
                if item["parent_uuid"] == org_uuid:
                    item["parent_uuid"] = None

            obj = await format_managers_and_location(obj)

            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_engagements(self):
        obj = await self._fetch_engagements()
        self.engagements.update(obj)

    async def _fetch_engagements(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching engagements")

        def collect_extensions(d: dict):
            for ext_obj in d["obj"]:
                if ext_obj is None:
                    continue
                ed = {}
                for i in range(1, 11):
                    ed[f"udvidelse_{i}"] = ext_obj.pop(f"extension_{i}")

                ext_obj["extensions"] = ed

            return d

        if self.full_history:
            query = """
                query (
                    $filter: EngagementFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: engagements(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                fraction
                                user_key
                                engagement_type_uuid
                                primary_uuid
                                is_primary
                                job_function_uuid
                                extension_1
                                extension_2
                                extension_3
                                extension_4
                                extension_5
                                extension_6
                                extension_7
                                extension_8
                                extension_9
                                extension_10
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: EngagementFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: engagements(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                               uuid
                                employee_uuid
                                org_unit_uuid
                                fraction
                                user_key
                                engagement_type_uuid
                                primary_uuid
                                is_primary
                                job_function_uuid
                                extension_1
                                extension_2
                                extension_3
                                extension_4
                                extension_5
                                extension_6
                                extension_7
                                extension_8
                                extension_9
                                extension_10
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "employee_uuid": "user",
            "engagement_type_uuid": "engagement_type",
            "job_function_uuid": "job_function",
            "org_unit_uuid": "unit",
            "primary_uuid": "primary_type",
            "is_primary": "primary_boolean",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = collect_extensions(obj)
            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_leaves(self):
        obj = await self._fetch_leaves()
        self.leaves.update(obj)

    async def _fetch_leaves(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching leaves")
        if self.full_history:
            query = """
                query (
                    $filter: LeaveFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: leaves(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                employee_uuid
                                user_key
                                leave_type_uuid
                                engagement_uuid
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: LeaveFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: leaves(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                employee_uuid
                                user_key
                                leave_type_uuid
                                engagement_uuid
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        replace_dictionary = {
            "employee_uuid": "user",
            "leave_type_uuid": "leave_type",
            "engagement_uuid": "engagement",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = convert_dict(obj, replace_dict=replace_dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_it_connections(self):
        obj = await self._fetch_it_connections()
        self.it_connections.update(obj)

    async def _fetch_it_connections(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching it users")

        async def set_primary_boolean(res: dict) -> dict:
            for res_obj in res["obj"]:
                if res_obj is None:
                    continue
                prim = res_obj.pop("primary")
                if prim:
                    if prim["user_key"] == "primary":
                        res_obj["primary_boolean"] = True
                    else:
                        res_obj["primary_boolean"] = False
                else:
                    res_obj["primary_boolean"] = None

            return res

        if self.full_history:
            query = """
                query (
                    $filter: ITUserFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: itusers(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                user_key
                                external_id
                                itsystem_uuid
                                primary {
                                    user_key
                                }
                                engagement_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: ITUserFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: itusers(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                user_key
                                external_id
                                itsystem_uuid
                                primary {
                                    user_key
                                }
                                engagement_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "employee_uuid": "user",
            "itsystem_uuid": "itsystem",
            "org_unit_uuid": "unit",
            "user_key": "username",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = await set_primary_boolean(obj)
            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_kles(self):
        obj = await self._fetch_kles()
        self.kles.update(obj)

    async def _fetch_kles(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching KLEs")

        async def format_aspects(d: dict) -> dict:
            new_obj_list = []
            for kle_obj in d["obj"]:
                if kle_obj is None:
                    continue
                asp_list = []
                asps = kle_obj.pop("kle_aspect_uuids")
                for a_uuid in asps:
                    aspect = kle_obj.copy()
                    aspect["kle_aspect_uuid"] = a_uuid
                    asp_list.append(aspect)
                new_obj_list.extend(asp_list)
            d["obj"] = new_obj_list

            return d

        if self.full_history:
            query = """
                query (
                    $filter: KLEFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: kles(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                org_unit_uuid
                                kle_number_uuid
                                kle_aspect_uuids
                                user_key
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: KLEFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: kles(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                org_unit_uuid
                                kle_number_uuid
                                kle_aspect_uuids
                                user_key
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "kle_aspect_uuid": "kle_aspect",
            "kle_number_uuid": "kle_number",
            "org_unit_uuid": "unit",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = await format_aspects(obj)
            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_related(self):
        obj = await self._fetch_related()
        self.related.update(obj)

    async def _fetch_related(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching related")

        def format_related(d: dict):
            for rel_obj in d["obj"]:
                if rel_obj is None:
                    continue
                rel_uuids_list = rel_obj.pop("org_unit_uuids")
                for i in range(1, (len(rel_uuids_list) + 1)):
                    rel_obj[f"unit{i}_uuid"] = rel_uuids_list[i - 1]

            return d

        if self.full_history:
            query = """
                query (
                    $filter: RelatedUnitFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: related_units(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                org_unit_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: RelatedUnitFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: related_units(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                org_unit_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = format_related(obj)

            obj = convert_dict(obj)
            insert_obj(obj, res)
        return res

    async def _cache_lora_managers(self):
        obj = await self._fetch_managers()
        self.managers.update(obj)

    async def _fetch_managers(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching managers")
        if self.full_history:
            query = """
                query (
                    $filter: ManagerFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: managers(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                manager_type_uuid
                                manager_level_uuid
                                responsibility_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: ManagerFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: managers(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                manager_type_uuid
                                manager_level_uuid
                                responsibility_uuids
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        dictionary = {
            "employee_uuid": "user",
            "manager_level_uuid": "manager_level",
            "manager_type_uuid": "manager_type",
            "responsibility_uuids": "manager_responsibility",
            "org_unit_uuid": "unit",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = convert_dict(obj, replace_dict=dictionary)
            insert_obj(obj, res)
        return res

    async def _cache_lora_associations(self):
        obj = await self._fetch_associations()
        self.associations.update(obj)

    async def _fetch_associations(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching associations")

        async def process_associations_helper(res: dict) -> dict:
            for res_obj in res["obj"]:
                if res_obj is None:
                    continue
                # check primary
                prim = res_obj.pop("primary")
                if prim:
                    if prim["user_key"] == "primary":
                        res_obj["primary_boolean"] = True
                    else:
                        res_obj["primary_boolean"] = False
                else:
                    res_obj["primary_boolean"] = None

                if not res_obj["it_user_uuid"]:
                    res_obj["job_function_uuid"] = None
                # Resolve dynamic class - including parent class if relevant
                dynamic_class = res_obj.pop("dynamic_class")
                if dynamic_class:
                    res_obj["dynamic_class"] = (
                        f"{dynamic_class['parent']['name']} - {dynamic_class['name']}"
                        if dynamic_class["parent"]
                        else dynamic_class["name"]
                    )

            return res

        # TODO job_function_uuid  # Should be None if it_user_uuid is None,
        #  ask Mads why?! #48316 !764 5dc0d245 . Mads is always busy
        if self.full_history:
            query = """
                query (
                    $filter: AssociationFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: associations(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                user_key
                                association_type_uuid
                                it_user_uuid
                                job_function_uuid
                                validity {
                                    from
                                    to
                                }
                                primary {
                                    user_key
                                }
                                dynamic_class {
                                    name
                                    parent {
                                    name
                                    }
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: AssociationFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: associations(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                uuid
                                employee_uuid
                                org_unit_uuid
                                user_key
                                association_type_uuid
                                it_user_uuid
                                job_function_uuid
                                validity {
                                    from
                                    to
                                }
                                primary {
                                    user_key
                                }
                                dynamic_class {
                                    name
                                    parent {
                                    name
                                    }
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        replace_dict = {
            "employee_uuid": "user",
            "association_type_uuid": "association_type",
            "it_user_uuid": "it_user",
            "job_function_uuid": "job_function",
            "org_unit_uuid": "unit",
        }

        res: dict = {}

        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            obj = await process_associations_helper(obj)
            obj = convert_dict(obj, replace_dict=replace_dict)

            insert_obj(obj, res)
        return res

    async def _cache_lora_address(self):
        obj = await self._fetch_address()
        self.addresses.update(obj)

    async def _fetch_address(self, uuid: UUID | None = None) -> dict:
        logger.info("Caching addresses")

        async def prep_address(d: dict) -> dict:
            for add_obj in d["obj"]:
                if add_obj is None:
                    continue
                scope = add_obj.pop("address_type")["scope"]

                add_obj["scope"] = scope_map[scope]

                if scope == "DAR":
                    add_obj["dar_uuid"] = add_obj["value"]
                    # We need to populate the dar cache, but do not want to
                    # resolve all addresses again, and the resolved dar address we do have is
                    # closest to betegnelse.
                    # We are willing to overwrite an address if it is already present, as it is the
                    # same address for each uuid
                    self.dar_cache[add_obj["value"]] = {"betegnelse": add_obj["name"]}
                else:
                    add_obj["dar_uuid"] = None

                if self.resolve_dar:
                    add_obj["value"] = add_obj.pop("name")
                else:
                    add_obj.pop("name")

            return d

        if self.full_history:
            query = """
                query (
                    $filter: AddressFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: addresses(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: validities {
                                address_type_uuid
                                employee_uuid
                                org_unit_uuid
                                engagement_uuid
                                ituser_uuid
                                visibility_uuid
                                name
                                value
                                uuid
                                user_key
                                address_type {
                                    scope
                                }
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                    "from_date": str(datetime.date.today()) if self.skip_past else None,
                    "to_date": None,
                },
            }
        else:
            query = """
                query (
                    $filter: AddressFilter
                    $limit: int
                    $cursor: Cursor
                ) {
                    page: addresses(
                        filter: $filter
                        limit: $limit
                        cursor: $cursor
                    ) {
                        objects {
                            uuid
                            obj: current {
                                address_type_uuid
                                employee_uuid
                                org_unit_uuid
                                engagement_uuid
                                ituser_uuid
                                visibility_uuid
                                name
                                value
                                uuid
                                user_key
                                address_type {
                                    scope
                                }
                                validity {
                                    from
                                    to
                                }
                            }
                        }
                        page_info {
                            next_cursor
                        }
                    }
                }
            """
            variables = {
                "filter": {
                    "uuids": str(uuid) if uuid is not None else None,
                },
            }

        scope_map = {
            "EMAIL": "E-mail",
            "WWW": "Url",
            "PHONE": "Telefon",
            "PNUMBER": "P-nummer",
            "EAN": "EAN",
            "TEXT": "Text",
            "MULTIFIELD_TEXT": "Multifield_text",
            "DAR": "DAR",
        }

        replace_dict = {
            "employee_uuid": "user",
            "org_unit_uuid": "unit",
            "engagement_uuid": "engagement",
            "ituser_uuid": "ituser",
            "address_type_uuid": "adresse_type",
            "visibility_uuid": "visibility",
        }

        res: dict = {}
        async for obj in self._execute_query(
            query=query, variable_values=variables, do_paged=uuid is None
        ):
            if obj is None:
                return {}
            if not self.full_history:
                obj = align_current(obj)

            # Skip if both of the below are None, should really be an invariant in MO
            # employee_uuid
            # org_unit_uuid
            if any(
                map(
                    lambda o: (
                        o is not None
                        and o["employee_uuid"] is None
                        and o["org_unit_uuid"] is None
                    ),
                    obj["obj"],
                )
            ):
                continue

            obj = await prep_address(obj)
            obj = convert_dict(obj, replace_dict=replace_dict)

            insert_obj(obj, res)
        return res

    async def populate_cache_async(self, dry_run=None, skip_associations=False):
        """
        Perform the actual data import.
        :param skip_associations: If associations are not needed, they can be
        skipped for increased performance.
        :param dry_run: For testing purposes it is possible to read from cache.
        """
        if dry_run is None:
            dry_run = os.environ.get("USE_CACHED_LORACACHE", False)

        # Ensure that tmp/ exists
        Path("tmp/").mkdir(exist_ok=True)

        if self.full_history:
            if self.skip_past:
                facets_file = "tmp/facets_historic_skip_past.p"
                classes_file = "tmp/classes_historic_skip_past.p"
                users_file = "tmp/users_historic_skip_past.p"
                units_file = "tmp/units_historic_skip_past.p"
                addresses_file = "tmp/addresses_historic_skip_past.p"
                engagements_file = "tmp/engagements_historic_skip_past.p"
                managers_file = "tmp/managers_historic_skip_past.p"
                associations_file = "tmp/associations_historic_skip_past.p"
                leaves_file = "tmp/leaves_historic_skip_past.p"
                itsystems_file = "tmp/itsystems_historic_skip_past.p"
                it_connections_file = "tmp/it_connections_historic_skip_past.p"
                kles_file = "tmp/kles_historic_skip_past.p"
                related_file = "tmp/related_historic_skip_past.p"
                dar_file = "tmp/dar_historic_skip_past.p"
            else:
                facets_file = "tmp/facets_historic.p"
                classes_file = "tmp/classes_historic.p"
                users_file = "tmp/users_historic.p"
                units_file = "tmp/units_historic.p"
                addresses_file = "tmp/addresses_historic.p"
                engagements_file = "tmp/engagements_historic.p"
                managers_file = "tmp/managers_historic.p"
                associations_file = "tmp/associations_historic.p"
                leaves_file = "tmp/leaves_historic.p"
                itsystems_file = "tmp/itsystems_historic.p"
                it_connections_file = "tmp/it_connections_historic.p"
                kles_file = "tmp/kles_historic.p"
                related_file = "tmp/related_historic.p"
                dar_file = "tmp/dar_historic.p"
        else:
            facets_file = "tmp/facets.p"
            classes_file = "tmp/classes.p"
            users_file = "tmp/users.p"
            units_file = "tmp/units.p"
            addresses_file = "tmp/addresses.p"
            engagements_file = "tmp/engagements.p"
            managers_file = "tmp/managers.p"
            associations_file = "tmp/associations.p"
            leaves_file = "tmp/leaves.p"
            itsystems_file = "tmp/itsystems.p"
            it_connections_file = "tmp/it_connections.p"
            kles_file = "tmp/kles.p"
            related_file = "tmp/related.p"
            dar_file = "tmp/dar.p"

        if dry_run:
            with open(facets_file, "rb") as f:
                self.facets = pickle.load(f)
            with open(classes_file, "rb") as f:
                self.classes = pickle.load(f)
            with open(users_file, "rb") as f:
                self.users = pickle.load(f)
            with open(units_file, "rb") as f:
                self.units = pickle.load(f)
            with open(addresses_file, "rb") as f:
                self.addresses = pickle.load(f)
            with open(engagements_file, "rb") as f:
                self.engagements = pickle.load(f)
            with open(managers_file, "rb") as f:
                self.managers = pickle.load(f)

            if not skip_associations:
                with open(associations_file, "rb") as f:
                    self.associations = pickle.load(f)

            with open(leaves_file, "rb") as f:
                self.leaves = pickle.load(f)
            with open(itsystems_file, "rb") as f:
                self.itsystems = pickle.load(f)
            with open(it_connections_file, "rb") as f:
                self.it_connections = pickle.load(f)
            with open(kles_file, "rb") as f:
                self.kles = pickle.load(f)
            with open(related_file, "rb") as f:
                self.related = pickle.load(f)
            with open(dar_file, "rb") as f:
                self.dar_cache = pickle.load(f)
            return

        # `tasks` is used to keep strong references. Otherwise, it can be
        # cleared by the garbage collector mid-execution as the event loop
        # only keeps weak references.
        tasks = []
        async with asyncio.TaskGroup() as tg:
            tasks.append(tg.create_task(self._cache_lora_address()))
            tasks.append(tg.create_task(self._cache_lora_units()))
            tasks.append(tg.create_task(self._cache_lora_engagements()))
            tasks.append(tg.create_task(self._cache_lora_facets()))
            tasks.append(tg.create_task(self._cache_lora_classes()))
            tasks.append(tg.create_task(self._cache_lora_users()))
            tasks.append(tg.create_task(self._cache_lora_managers()))
            if not skip_associations:
                tasks.append(tg.create_task(self._cache_lora_associations()))
            tasks.append(tg.create_task(self._cache_lora_leaves()))
            tasks.append(tg.create_task(self._cache_lora_itsystems()))
            tasks.append(tg.create_task(self._cache_lora_it_connections()))
            tasks.append(tg.create_task(self._cache_lora_kles()))
            tasks.append(tg.create_task(self._cache_lora_related()))
        del tasks

        def write_caches(cache, filename, name):
            logger.debug(f"writing {name}")
            if filename:
                with open(filename, "wb") as fw:
                    pickle.dump(cache, fw, pickle.DEFAULT_PROTOCOL)
            logger.debug(f"done with {name}")

        write_caches(self.facets, facets_file, "facets")
        write_caches(self.engagements, engagements_file, "engagements")
        write_caches(self.classes, classes_file, "classes")
        write_caches(self.users, users_file, "users")
        write_caches(self.units, units_file, "units")
        write_caches(self.managers, managers_file, "managers")
        write_caches(self.leaves, leaves_file, "leaves")
        write_caches(self.addresses, addresses_file, "addresses")
        write_caches(self.dar_cache, dar_file, "dar_addresses")
        write_caches(self.itsystems, itsystems_file, "itsystems")
        write_caches(self.it_connections, it_connections_file, "it_connections")
        write_caches(self.kles, kles_file, "kles")
        write_caches(self.related, related_file, "related")

        if not skip_associations:
            write_caches(self.associations, associations_file, "associations")

    @async_to_sync
    async def populate_cache(self, dry_run=None, skip_associations=False):
        logger.info(f"Populating cache {dry_run=} {skip_associations=}")
        await self.populate_cache_async(
            dry_run=dry_run, skip_associations=skip_associations
        )

    def calculate_primary_engagements(self):
        # Needed for compatibility reasons
        pass

    def calculate_derived_unit_data(self):
        # Needed for compatibility reasons
        pass
