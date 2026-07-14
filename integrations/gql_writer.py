# SPDX-FileCopyrightText: 2023 Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""GraphQL writer helper for OS2mo mutations.

Wraps a sync ``GraphQLClient`` and exposes ``create`` / ``update`` /
``terminate`` methods that dispatch to the corresponding ``*_create`` /
``*_update`` / ``*_terminate`` GraphQL mutations.  Replaces the REST
``MoraHelper._mo_post`` write path.
"""

import logging
from typing import Dict
from uuid import UUID

from fastramqpi.raclients.graph.client import GraphQLClient
from gql import gql
from graphql import DocumentNode
from more_itertools import first

logger = logging.getLogger("gql_writer")

_DRY_RUN_UUID = "00000000-0000-0000-0000-000000000000"
_DEFAULT_CLASS_VALIDITY = {"from": "1900-01-01", "to": None}

# ---------------------------------------------------------------------------
# Mutation string constants
# ---------------------------------------------------------------------------
# Shapes copied from ``exporters/sql_export/tests/integration/conftest.py``.
# Field naming is snake_case (the OS2mo schema disables auto-camelCase).

ADDRESS_CREATE = gql(
    """
    mutation CreateAddress($input: AddressCreateInput!) {
        address_create(input: $input) { uuid }
    }
    """
)
ADDRESS_UPDATE = gql(
    """
    mutation UpdateAddress($input: AddressUpdateInput!) {
        address_update(input: $input) { uuid }
    }
    """
)
ADDRESS_TERMINATE = gql(
    """
    mutation TerminateAddress($input: AddressTerminateInput!) {
        address_terminate(input: $input) { uuid }
    }
    """
)

ENGAGEMENT_CREATE = gql(
    """
    mutation CreateEngagement($input: EngagementCreateInput!) {
        engagement_create(input: $input) { uuid }
    }
    """
)
ENGAGEMENT_UPDATE = gql(
    """
    mutation UpdateEngagement($input: EngagementUpdateInput!) {
        engagement_update(input: $input) { uuid }
    }
    """
)
ENGAGEMENT_TERMINATE = gql(
    """
    mutation TerminateEngagement($input: EngagementTerminateInput!) {
        engagement_terminate(input: $input) { uuid }
    }
    """
)

EMPLOYEE_CREATE = gql(
    """
    mutation CreateEmployee($input: EmployeeCreateInput!) {
        employee_create(input: $input) { uuid }
    }
    """
)
EMPLOYEE_UPDATE = gql(
    """
    mutation UpdateEmployee($input: EmployeeUpdateInput!) {
        employee_update(input: $input) { uuid }
    }
    """
)
EMPLOYEE_TERMINATE = gql(
    """
    mutation TerminateEmployee($input: EmployeeTerminateInput!) {
        employee_terminate(input: $input) { uuid }
    }
    """
)

ORG_UNIT_CREATE = gql(
    """
    mutation CreateOrgUnit($input: OrganisationUnitCreateInput!) {
        org_unit_create(input: $input) { uuid }
    }
    """
)
ORG_UNIT_UPDATE = gql(
    """
    mutation UpdateOrgUnit($input: OrganisationUnitUpdateInput!) {
        org_unit_update(input: $input) { uuid }
    }
    """
)
ORG_UNIT_TERMINATE = gql(
    """
    mutation TerminateOrgUnit($input: OrganisationUnitTerminateInput!) {
        org_unit_terminate(input: $input) { uuid }
    }
    """
)

ITUSER_CREATE = gql(
    """
    mutation CreateITUser($input: ITUserCreateInput!) {
        ituser_create(input: $input) { uuid }
    }
    """
)
ITUSER_UPDATE = gql(
    """
    mutation UpdateITUser($input: ITUserUpdateInput!) {
        ituser_update(input: $input) { uuid }
    }
    """
)
ITUSER_TERMINATE = gql(
    """
    mutation TerminateITUser($input: ITUserTerminateInput!) {
        ituser_terminate(input: $input) { uuid }
    }
    """
)

MANAGER_CREATE = gql(
    """
    mutation CreateManager($input: ManagerCreateInput!) {
        manager_create(input: $input) { uuid }
    }
    """
)
MANAGER_UPDATE = gql(
    """
    mutation UpdateManager($input: ManagerUpdateInput!) {
        manager_update(input: $input) { uuid }
    }
    """
)
MANAGER_TERMINATE = gql(
    """
    mutation TerminateManager($input: ManagerTerminateInput!) {
        manager_terminate(input: $input) { uuid }
    }
    """
)

CLASS_CREATE = gql(
    """
    mutation CreateClass($input: ClassCreateInput!) {
        class_create(input: $input) { uuid }
    }
    """
)
CLASS_UPDATE = gql(
    """
    mutation UpdateClass($input: ClassUpdateInput!) {
        class_update(input: $input) { uuid }
    }
    """
)
CLASS_TERMINATE = gql(
    """
    mutation TerminateClass($input: ClassTerminateInput!) {
        class_terminate(input: $input) { uuid }
    }
    """
)

# ---------------------------------------------------------------------------
# Query string constants (for ``ensure_class_in_facet``)
# ---------------------------------------------------------------------------

CLASSES_IN_FACET = gql(
    """
    query ClassesInFacet($facet_user_keys: [String!]) {
        classes(filter: {facet_user_keys: $facet_user_keys}) {
            objects {
                current {
                    uuid
                    user_key
                    facet_uuid
                }
            }
        }
    }
    """
)

FACET_BY_USER_KEY = gql(
    """
    query FacetByUserKey($user_keys: [String!]) {
        facets(filter: {user_keys: $user_keys}) {
            objects {
                current {
                    uuid
                }
            }
        }
    }
    """
)

# ---------------------------------------------------------------------------
# entity_type -> mutation mapping tables (1.7)
# ---------------------------------------------------------------------------

_CREATE_MUTATIONS: Dict[str, DocumentNode] = {
    "address": ADDRESS_CREATE,
    "engagement": ENGAGEMENT_CREATE,
    "employee": EMPLOYEE_CREATE,
    "org_unit": ORG_UNIT_CREATE,
    "ituser": ITUSER_CREATE,
    "manager": MANAGER_CREATE,
    "class": CLASS_CREATE,
}

_UPDATE_MUTATIONS: Dict[str, DocumentNode] = {
    "address": ADDRESS_UPDATE,
    "engagement": ENGAGEMENT_UPDATE,
    "employee": EMPLOYEE_UPDATE,
    "org_unit": ORG_UNIT_UPDATE,
    "ituser": ITUSER_UPDATE,
    "manager": MANAGER_UPDATE,
    "class": CLASS_UPDATE,
}

_TERMINATE_MUTATIONS: Dict[str, DocumentNode] = {
    "address": ADDRESS_TERMINATE,
    "engagement": ENGAGEMENT_TERMINATE,
    "employee": EMPLOYEE_TERMINATE,
    "org_unit": ORG_UNIT_TERMINATE,
    "ituser": ITUSER_TERMINATE,
    "manager": MANAGER_TERMINATE,
    "class": CLASS_TERMINATE,
}


class GraphQLWriter:
    """GraphQL mutation dispatcher for OS2mo writes.

    Accepts a pre-built sync ``GraphQLClient`` (reusing the pattern from
    ``opus_diff_import.py`` or ``ad_writer.py``), or auth params to construct
    one.  When ``dry_run`` is set, ``create`` / ``update`` / ``terminate``
    log the mutation and input then return a fake UUID without executing.
    """

    def __init__(
        self,
        gql_client: GraphQLClient | None = None,
        dry_run: bool = False,
        url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        auth_realm: str = "mo",
        auth_server: str | None = None,
    ):
        self.dry_run = dry_run
        if gql_client is not None:
            self._gql_client = gql_client
        else:
            self._gql_client = GraphQLClient(
                url=url,
                client_id=client_id,
                client_secret=client_secret,
                auth_realm=auth_realm,
                auth_server=auth_server,
                sync=True,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(
        self,
        mutation: DocumentNode,
        input_data: dict,
        entity_type: str,
        op: str,
    ) -> str:
        """Execute *mutation* and return the UUID from the response.

        :param op: One of ``"create"``, ``"update"``, ``"terminate"``.
        """
        if self.dry_run:
            logger.info("dry-run %s %s input=%s", op, entity_type, input_data)
            return _DRY_RUN_UUID
        resp = self._gql_client.execute(mutation, variable_values={"input": input_data})
        return resp[f"{entity_type}_{op}"]["uuid"]

    # ------------------------------------------------------------------
    # Public API (1.3 - 1.5)
    # ------------------------------------------------------------------

    def create(self, entity_type: str, input_data: dict) -> str:
        """Dispatch to ``{entity_type}_create`` and return the new UUID."""
        return self._execute(
            _CREATE_MUTATIONS[entity_type], input_data, entity_type, "create"
        )

    def update(self, entity_type: str, input_data: dict) -> str:
        """Dispatch to ``{entity_type}_update`` and return the UUID."""
        return self._execute(
            _UPDATE_MUTATIONS[entity_type], input_data, entity_type, "update"
        )

    def terminate(
        self,
        entity_type: str,
        uuid: str,
        to_date: str,
        from_date: str | None = None,
    ) -> str:
        """Dispatch to ``{entity_type}_terminate``.

        :param to_date: End date (validity ``to``), required.
        :param from_date: Optional interval start (validity ``from``).
        """
        validity: dict = {"to": to_date}
        if from_date is not None:
            validity["from"] = from_date
        input_data = {"uuid": uuid, "validity": validity}
        return self._execute(
            _TERMINATE_MUTATIONS[entity_type],
            input_data,
            entity_type,
            "terminate",
        )

    # ------------------------------------------------------------------
    # ensure_class_in_facet (1.6)
    # ------------------------------------------------------------------

    def ensure_class_in_facet(
        self,
        facet: str,
        bvn: str,
        title: str | None = None,
        scope: str | None = "TEXT",
        owner: UUID | None = None,
    ) -> str:
        """Ensure a class exists in *facet* and return its UUID.

        Queries existing classes in the facet via GraphQL.  If a matching
        ``user_key`` (bvn) is found, returns its UUID.  Otherwise creates the
        class via ``class_create`` and returns the new UUID.

        Replaces ``MoraHelper.ensure_class_in_facet`` (REST read + conditional
        REST create).
        """
        class_title = title or bvn

        resp = self._gql_client.execute(
            CLASSES_IN_FACET,
            variable_values={"facet_user_keys": [facet]},
        )
        classes = [
            obj["current"] for obj in resp["classes"]["objects"] if obj.get("current")
        ]

        matches = [c for c in classes if c.get("user_key", "").lower() == bvn.lower()]
        if matches:
            if len(matches) > 1:
                logger.warning(
                    "More than one class matched bvn %r in facet %r. "
                    "Picked %s from: %s",
                    bvn,
                    facet,
                    matches[0]["uuid"],
                    matches,
                )
            return matches[0]["uuid"]

        # No match - need the facet UUID to build ClassCreateInput.
        facet_uuid = first(
            (c["facet_uuid"] for c in classes if c.get("facet_uuid")),
            default=None,
        )
        if facet_uuid is None:
            facet_resp = self._gql_client.execute(
                FACET_BY_USER_KEY,
                variable_values={"user_keys": [facet]},
            )
            facet_uuid = first(
                (
                    obj["current"]["uuid"]
                    for obj in facet_resp["facets"]["objects"]
                    if obj.get("current")
                ),
                default=None,
            )
            if facet_uuid is None:
                raise ValueError(f"Facet {facet!r} not found")

        payload: dict = {
            "name": class_title,
            "user_key": bvn,
            "facet_uuid": facet_uuid,
            "published": "Publiceret",
            "validity": _DEFAULT_CLASS_VALIDITY,
        }
        if scope is not None:
            payload["scope"] = scope
        if owner is not None:
            payload["owner"] = str(owner)

        return self.create("class", payload)
