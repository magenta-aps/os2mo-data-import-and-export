# SPDX-FileCopyrightText: 2023 Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
##
# Copyright (c) 2017-2018, Magenta ApS
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
"""
Helper class to make a number of pre-defined queries into MO
"""

import csv
import logging
from functools import lru_cache
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

import requests
from anytree import Node
from fastramqpi.ra_utils.headers import TokenSettings
from more_itertools import first
from more_itertools import only
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_random_exponential

PRIMARY_RESPONSIBILITY = "Personale: ansættelse/afskedigelse"

logger = logging.getLogger("mora-helper")


class MoraHelper:
    def __init__(
        self,
        hostname="http://localhost:5000",
        use_cache=True,
        client_id: str | None = None,
        client_secret: str | None = None,
        auth_server: str | None = None,
        auth_realm: str | None = None,
    ):
        self.host = hostname + "/service/"
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_server = auth_server
        self.auth_realm = auth_realm
        self.cache = {}
        self.default_cache = use_cache

    def get_auth_headers(self):
        if self.client_secret is None:
            return TokenSettings().get_headers()

        return TokenSettings(
            client_id=self.client_id,
            client_secret=self.client_secret,
            auth_server=self.auth_server,
            auth_realm=self.auth_realm,
        ).get_headers()

    def _split_name(self, name):
        """Split a name into first and last name.
        Currently just splits at last space, but this might turn out to
        be a too simple rule.
        :param name: The name to split.
        :return: Dict with first and last name separated.
        """
        splitted_name = {
            "Fornavn": name[: name.rfind(" ")],
            "Efternavn": name[name.rfind(" ") :],
        }
        return splitted_name

    def _read_node_path(self, node):
        """Find the full path for a given node
        :param node: The node to find the path for
        :return: List with full path for the node
        """
        path = []
        for sub_node in node.path:
            ou = self.read_ou(sub_node.name)
            path += [ou["name"]]
        return path

    def _create_fieldnames(self, nodes):
        """Create a list of fieldnames that has a suitable length
        to accomodate the full tree debth. First names are hard-coded
        the rest are auto-generated
        :param nodes: List of all nodes in tree
        :return: A list of fieldnames
        """
        fieldnames = ["root", "org", "sub org"]
        for i in range(2, nodes["root"].height):
            fieldnames += [str(i) + "xsub org"]
        return fieldnames

    def _write_csv(self, fieldnames, rows, filename):
        """Write a csv-file from a a dataset. Only fields explicitly mentioned
        in fieldnames will be saved, the rest will be ignored.
        :param fieldnames: The headline for the columns, also act as filter.
        :param rows: A list of dicts, each list element will become a row, keys
        in dict will be matched to fieldnames.
        :param filename: The name of the exported file.
        """
        with open(filename, encoding="utf-8", mode="w") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=fieldnames,
                extrasaction="ignore",
                delimiter=";",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _create_path_dict(self, fieldnames, node, org_types=None):
        """Create a dict with a MO-path to a given node.

        :param fieldnames: The headline for each level of the path.
        :node: The node to find the path for.
        :return: A dict with headlines as keys and nodes as values.
        """
        ou = self.read_ou(node.name)
        ou_type = ou["org_unit_type"]["name"]

        if org_types and (ou_type not in org_types):
            return None

        path = self._read_node_path(node)
        path_dict = {}
        i = 0
        for element in path:
            path_dict[fieldnames[i]] = element
            i += 1
        return path_dict

    @retry(
        retry=retry_if_exception_type(
            (requests.exceptions.HTTPError, requests.exceptions.ConnectionError)
        ),
        wait=wait_random_exponential(multiplier=1, max=5),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _mo_lookup(
        self,
        uuid,
        url,
        at=None,
        validity=None,
        only_primary=False,
        use_cache=None,
        calculate_primary=False,
    ):
        # TODO: at-value is currently not part of cache key
        if use_cache is None:
            use_cache = self.default_cache

        params = {}
        if calculate_primary:
            params["calculate_primary"] = 1
        if only_primary:
            params["only_primary_uuid"] = 1
        if at:
            params["at"] = at
        elif validity:
            params["validity"] = validity

        full_url = self.host + url.format(uuid)
        cache_id = full_url + str(validity)
        if (cache_id in self.cache) and use_cache:
            logger.debug("cache hit: %s", cache_id)
            return_dict = self.cache[cache_id]
        else:
            headers = self.get_auth_headers()

            response = requests.get(full_url, headers=headers, params=params)
            if response.status_code == 401:
                msg = "Missing Authorization"
                if headers:
                    msg = "Authorization not accepted"
                logger.error(msg)
                raise requests.exceptions.RequestException(msg)

            if (response.status_code == 500) and (
                "has been deleted" not in response.text
            ):
                response.raise_for_status()

            return_dict = response.json()
            self.cache[cache_id] = return_dict

        return return_dict

    def _mo_post(self, url, payload, force=True):
        params = {}
        if force:
            params["force"] = 1

        headers = self.get_auth_headers()

        full_url = self.host + url
        response = requests.post(full_url, headers=headers, params=params, json=payload)
        return response

    @lru_cache
    def read_organisation(self):
        """Read the main Organisation, all OU's will have this as root

        :return: UUID of root organisation
        """
        org_id = self._mo_lookup(uuid=None, url="o/")
        return org_id[0]["uuid"]

    def read_all_users(self, limit: int = 1_000, at=None) -> List[dict]:
        """Return a list of all employees in MO.

        :param limit: Size of pagination groups. Set to 0 to skip pagination and fetch
            all users in one request.
        :return: List af all employees.
        """
        org = self.read_organisation()
        start = 0
        total = 1
        all_employees = []
        while start < total:
            employee_list = self._mo_lookup(
                org, f"o/{{}}/e/?limit={limit}&start={start}", at=at
            )
            all_employees.extend(employee_list["items"])
            start = employee_list["offset"] + limit
            total = employee_list["total"]
        return all_employees

    def read_it_systems(self):
        """Read all it systems

        :return: list of it systems, eg. Opus, Active Directory
        """
        org_id = self.read_organisation()
        it_systems = self._mo_lookup(org_id, url="o/{}/it/")
        return it_systems

    def read_ou(self, uuid, at=None, use_cache=None) -> Dict:
        """Return a dict with the data available about an OU.

        :param uuid: The UUID of the OU
        :return: Dict with the information about the OU
        """
        org_enhed = self._mo_lookup(uuid, "ou/{}/", at, use_cache)
        return org_enhed

    def read_ou_address(
        self,
        uuid,
        at=None,
        use_cache=None,
        scope="DAR",
        return_all=False,
        reformat=True,
    ):
        """Return a dict with the data available about an OU.

        :param uuid: The UUID of the OU
        :param return_all: If True the response will be a list of dicts
        rather than a dict, and all adresses will be returned.
        :return: Dict (or list) with the information about the OU
        """

        def reformat_address(address):
            return {
                "type": address["address_type"]["uuid"],
                "visibility": address.get("visibility"),
                # Deprecated spelling mistake....
                "visibibility": address.get("visibility"),
                "Adresse": address["name"],
                "value": address["value"],
                "uuid": address["uuid"],
            }

        addresses = self._mo_lookup(uuid, "ou/{}/details/address", at, use_cache)
        if scope is not None:
            addresses = filter(
                lambda address: address["address_type"]["scope"] == scope, addresses
            )
        # TODO: Eliminiate this random intermediate format?
        if reformat:
            addresses = map(reformat_address, addresses)
        return_list = list(addresses)

        if return_all:
            return return_list
        if return_list:
            return return_list[0]
        return {}

    def read_classes_in_facet(self, facet, use_cache=False):
        """Return all classes belong to a given facet.

        :param facet: The facet to be returned.
        :return: List of classes in the facet and the uuid of the facet.
        """
        org_uuid = self.read_organisation()
        url = "o/" + org_uuid + "/f/{}/"
        class_list = self._mo_lookup(facet, url, use_cache=False)
        classes = class_list["data"]["items"]
        facet_uuid = class_list["uuid"]
        return (classes, facet_uuid)

    @lru_cache(maxsize=None)
    def ensure_class_in_facet(
        self,
        facet: str,
        bvn: str,
        title: Optional[str] = None,
        scope: str = "TEXT",
        uuid: Optional[UUID] = None,
        owner: Optional[UUID] = None,
    ) -> UUID:
        """Ensures class exists in given facet."""

        classes, _ = self.read_classes_in_facet(facet)
        class_title = title or bvn
        # Check if title or bvn matches existing class
        current = list(
            filter(
                lambda c: c["name"].lower() == class_title.lower()
                or c["user_key"].lower() == bvn.lower(),
                classes,
            )
        )

        if current:
            current_uuid = UUID(first(current)["uuid"])
            if len(current) > 1:
                print(
                    f"More than one class matched on title or bvn. "
                    f"Picked {current_uuid} from: {current}"
                )
            return current_uuid

        org_uuid = self.read_organisation()
        payload = {
            "name": class_title,
            "user_key": bvn,
            "scope": scope,
            "org_uuid": org_uuid,
            "owner": str(owner) if owner else None,
        }
        url = f"f/{facet}/"
        if uuid:
            payload.update({"uuid": str(uuid)})
        r = self._mo_post(url, payload)
        r.raise_for_status()
        return UUID(r.json())

    def read_user(
        self, user_uuid=None, user_cpr=None, at=None, use_cache=None, org_uuid=None
    ):
        """Read basic info for a user. Either uuid or cpr must be given.

        :param user_uuid: UUID of the wanted user.
        :param user_cpr: cpr of the wanted user.
        :return: Basic user info
        """
        user_info = None
        if user_uuid:
            user_info = self._mo_lookup(user_uuid, "e/{}/", at, use_cache)
        if user_cpr:
            if not org_uuid:
                org_uuid = self.read_organisation()
            user = self._mo_lookup(
                user_cpr, "o/" + org_uuid + "/e/?query={}", at, use_cache
            )
            assert user["total"] < 2  # Only a single person can be found from cpr

            if user["total"] == 1:
                user_info = self._mo_lookup(
                    user["items"][0]["uuid"], "e/{}/", at, use_cache
                )
        return user_info

    def read_user_engagement(self, *args, **kwargs):
        return self.read_user_engagements(*args, **kwargs)

    def read_user_engagements(
        self,
        user,
        at=None,
        read_all=False,
        skip_past=False,
        only_primary=False,
        use_cache=None,
        calculate_primary=False,
    ):
        """Read engagements for a user.

        :param user: UUID of the wanted user.
        :read_all: Read all engagements, not only the present ones.
        :skip_past: Even if read_all is true, do not read the past.
        :calculate_primary: If True, ask MO to calculate primary engagement status.
        :return: List of the users engagements.
        """
        if not read_all:
            engagements = self._mo_lookup(
                user,
                "e/{}/details/engagement",
                at,
                only_primary=only_primary,
                use_cache=use_cache,
                calculate_primary=calculate_primary,
            )
        else:
            if skip_past:
                validity_times = ["present", "future"]
            else:
                validity_times = ["past", "present", "future"]

            engagements = []
            for validity in validity_times:
                engagement = self._mo_lookup(
                    user,
                    "e/{}/details/engagement",
                    validity=validity,
                    only_primary=only_primary,
                    use_cache=False,
                    calculate_primary=calculate_primary,
                )
                if isinstance(engagement, list):
                    engagements = engagements + engagement
                else:
                    print(f"There was an unexpected engagement returned: {engagement}")
        return engagements

    def read_user_address(
        self,
        user,
        username=False,
        cpr=False,
        at=None,
        use_cache=None,
        phone_type=None,
        email_type=None,
    ):
        """Read phone number and email from user.

        :param user: UUID of the wanted user
        :param phone_type: Optionally add a specific phone_type class.
        :return: Dict witn phone number and email (if they exists in MO)
        """
        addresses = self._mo_lookup(user, "e/{}/details/address", at, use_cache)
        return_address = {}
        for address in addresses:
            if address["address_type"]["scope"] == "PHONE":
                if phone_type is None:
                    return_address["Telefon"] = address["name"]
                else:
                    if address["address_type"]["uuid"] == phone_type:
                        return_address["Telefon"] = address["name"]

            if address["address_type"]["scope"] == "EMAIL":
                if email_type is None:
                    return_address["E-mail"] = address["name"]
                else:
                    if address["address_type"]["uuid"] == email_type:
                        return_address["E-mail"] = address["name"]
        if username or cpr:
            personal_info = self._mo_lookup(user, "e/{}/")
            if username:
                return_address["Brugernavn"] = personal_info["user_key"]
            if cpr:
                return_address["CPR-Nummer"] = personal_info["cpr_no"]
        return return_address

    def read_user_manager_status(self, user):
        """Returns True of user has responsibility:
        'Personale: ansættelse/afskedigelse'
        :param user: UUID of the wanted user
        :return: True if person is manager accoring to above mentioned rule
        """
        manager_functions = self._mo_lookup(user, "e/{}/details/manager")
        for manager_function in manager_functions:
            for responsibility in manager_function["responsibility"]:
                if responsibility["name"] == PRIMARY_RESPONSIBILITY:
                    return True
        return False

    def read_engagement_manager(self, engagement_uuid):
        """
        Read the manager corresponding to a given engagement.
        If the engagement user is not a manager, the manager will be the (possibly
        inhereted) manager of the unit. If the person is a manager, the manager will
        be the manager of the nearest higer level department. If no higher level
        managers exists, the person will be a self-manager (typically
        'Kommunaldirektør').
        """
        user_manager = None

        # XXX: Why is this here, this is for MOX?!
        url = "http://localhost:5000/lora/organisation/organisationfunktion/{}"
        response = requests.get(url.format(engagement_uuid))
        data = response.json()
        relationer = data[engagement_uuid][0]["registreringer"][0]["relationer"]
        user = relationer["tilknyttedebrugere"][0]
        unit = relationer["tilknyttedeenheder"][0]

        unit_manager = self.read_ou_manager(unit["uuid"], inherit=True)
        if unit_manager is None:
            raise Exception("Unable to find manager")

        if unit_manager["uuid"] != user["uuid"]:
            # In this case the engagement is not manager for itself
            user_manager = unit_manager
        else:
            mo_unit = self.read_ou(unit["uuid"])
            while unit_manager["uuid"] == user["uuid"]:
                if mo_unit["parent"] is None:
                    # Self manager!
                    break
                parent_uuid = mo_unit["parent"]["uuid"]
                unit_manager = self.read_ou_manager(parent_uuid, inherit=True)
                mo_unit = self.read_ou(parent_uuid)
            user_manager = unit_manager

        return user_manager

    def read_ou_manager(self, unit_uuid, inherit=False):
        """
        Read the manager of an organisation.
        Currently an exception will be raised if an ou has more than
        one manager.
        :param org_uuid: UUID of the OU to find the manager of
        :return: Returns 0 or 1 manager of the OU.
        """
        manager_list = {}
        if inherit:
            managers = self._mo_lookup(
                unit_uuid, "ou/{}/details/manager?inherit_manager=1"
            )
        else:
            managers = self._mo_lookup(unit_uuid, "ou/{}/details/manager")
        # Iterate over all managers, use uuid as key, if more than one
        # distinct uuid shows up in list, rasie an error
        for manager in managers:
            responsibility = {"name": "Intet ansvar"}
            for responsibility in manager["responsibility"]:
                if responsibility["name"] == PRIMARY_RESPONSIBILITY:
                    break
            # TODO: if primary reponsibility is found, this is now selected,
            # otherwise we simply use the last element in the list

            if manager["person"] is not None:
                uuid = manager["person"]["uuid"]
                data = {
                    "Navn": manager["person"]["name"],
                    # 'Ansvar': manager['responsibility'][0]['name'],
                    "Ansvar": responsibility["name"],
                    "uuid": uuid,
                }
                manager_list[uuid] = data
            else:
                # TODO: This is a vacant manager position
                pass
        if len(manager_list) == 0:
            manager = {}
        elif len(manager_list) == 1:
            manager = manager_list[uuid]
        elif len(manager_list) > 1:
            # Currently we do not support multiple managers
            logger.warning("multiple managers not supported for %s", unit_uuid)
            manager = manager_list[uuid]
            # TODO: Fix this...
            # raise Exception('Too many managers')
        return manager

    def read_organisation_people(
        self,
        org_uuid,
        function_type="engagement",
        split_name=True,
        read_all=False,
        skip_past=False,
    ):
        """Read all employees in an ou. If the same employee is listed
        more than once, only the latest listing will be included.
        :param org_uuid: UUID of the OU to find employees in.
        :read_all: Read all engagements, not only the present ones.
        :return: The list of employees
        """
        person_list = {}
        if not read_all:
            all_functions = self._mo_lookup(org_uuid, "ou/{}/details/" + function_type)
        else:
            if skip_past:
                validity_times = ["present", "future"]
            else:
                validity_times = ["past", "present", "future"]

            all_functions = []
            for validity in validity_times:
                functions = self._mo_lookup(
                    org_uuid, "ou/{}/details/" + function_type, validity=validity
                )
                all_functions = all_functions + functions

        for function in all_functions:
            if function["person"] is None:
                # Vacant association
                continue
            uuid = function["person"]["uuid"]
            data = {
                "Ansættelse gyldig fra": function["validity"]["from"],
                "Ansættelse gyldig til": function["validity"]["to"],
                "Person UUID": uuid,
                "Org-enhed": function["org_unit"]["name"],
                "Org-enhed UUID": function["org_unit"]["uuid"],
                "Engagement UUID": function["uuid"],
            }
            if "job_function" in function and function["job_function"] is not None:
                data["Stillingsbetegnelse"] = function["job_function"]["name"]
            else:
                data["Stillingsbetegnelse"] = None

            if (
                "engagement_type" in function
                and function["engagement_type"] is not None
            ):
                data["engagement_type_uuid"] = function["engagement_type"]["uuid"]
                data["Engagementstype"] = function["engagement_type"]["name"]
            else:
                data["engagement_type_uuid"] = None
                data["Engagementstype"] = None

            if (
                "association_type" in function
                and function["association_type"] is not None
            ):
                data["Post"] = function["association_type"]["name"]
            else:
                data["Post"] = None

            # Finally, add name
            if split_name:
                # If a split name i wanted, we prefer to get i directly from MO
                # rather than an algorithmic split.
                if function["person"].get("givenname"):
                    data["Fornavn"] = function["person"].get("givenname")
                    data["Efternavn"] = function["person"].get("surname")
                else:
                    data.update(self._split_name(function["person"]["name"]))
            else:
                data["Navn"] = function["person"]["name"]
            person_list[uuid] = data
        return person_list

    def read_top_units(self, organisation, use_cache=False):
        """Read the ous tha refers directoly to the organisation
        :param organisation: UUID of the organisation
        :return: List of UUIDs of the root OUs in the organisation
        """
        url = "o/{}/children"
        units = self._mo_lookup(organisation, url, use_cache=use_cache)
        return units

    def read_ou_tree(self, org, nodes={}, parent=None):
        """Recursively find all sub-ou's beneath current node
        :param org: The organisation to start the tree from
        :param nodes: Dict with all modes in the tree
        :param parent: The parent of the current node, None if this is root
        :return: A dict with all nodes in tree, top node is named 'root'
        """
        url = "ou/{}/children"
        units = self._mo_lookup(org, url)

        if parent is None:
            nodes["root"] = Node(org)
            parent = nodes["root"]
        for unit in units:
            uuid = unit["uuid"]
            nodes[uuid] = Node(uuid, parent=parent)
            if unit["child_count"] > 0:
                nodes = self.read_ou_tree(uuid, nodes, nodes[uuid])
        return nodes

    def read_ou_root(self, org: str, root: Optional[str] = None):
        """Find all org units below an optional root node
        :param org: The organisation to search in
        :param root: The UUID of an optional root node
        :return: A list of all org units bounded by the root node
        """
        url = f"o/{org}/ou/"
        if root:
            url += f"?root={root}"
        org_units = self._mo_lookup(None, url)["items"]
        return org_units

    def get_e_addresses(self, e_uuid, scope=None):
        addresses = self._mo_lookup(e_uuid, "e/{}/details/address")
        if scope:
            addresses = filter(
                lambda address: address["address_type"]["scope"] == scope, addresses
            )
        return list(addresses)

    def get_e_address(self, e_uuid, scope):
        return only(self.get_e_addresses(e_uuid, scope), {})

    def get_e_itsystems(self, e_uuid, it_system_uuid=None):
        it_systems = self._mo_lookup(e_uuid, "e/{}/details/it")
        if it_system_uuid:
            it_systems = filter(
                lambda its: its["itsystem"]["uuid"] == it_system_uuid, it_systems
            )
        return list(it_systems)

    def update_user(self, uuid, data):
        payload = {"type": "employee", "uuid": uuid, "data": data}
        return self._mo_post("details/edit", payload)


if __name__ == "__main__":
    # Example usage: env CLIENT_SECRET=SECRET_UUID_HERE python3 mora_helper.py
    mh = MoraHelper()
    print("Organisation UUID: " + mh.read_organisation())
    print("Number of users: " + str(len(mh.read_all_users())))
