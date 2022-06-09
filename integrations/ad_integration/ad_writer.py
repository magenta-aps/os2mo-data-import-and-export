# -*- coding: utf-8 -*-
import json
import logging
import os
import random
import re
import time
from abc import ABC
from abc import abstractmethod
from datetime import date
from datetime import datetime
from functools import lru_cache
from functools import partial
from operator import itemgetter

import click
import jmespath
from click_option_group import optgroup
from click_option_group import RequiredMutuallyExclusiveOptionGroup
from gql import gql
from jinja2 import Environment
from jinja2 import StrictUndefined
from jinja2 import Undefined
from more_itertools import first
from more_itertools import unzip
from os2mo_helpers.mora_helpers import MoraHelper
from ra_utils.lazy_dict import LazyDict
from ra_utils.lazy_dict import LazyEval
from ra_utils.lazy_dict import LazyEvalDerived
from raclients.graph.client import GraphQLClient

from . import ad_templates
from .ad_common import AD
from .ad_exceptions import CprNotFoundInADException
from .ad_exceptions import CprNotNotUnique
from .ad_exceptions import EngagementDatesError
from .ad_exceptions import NoActiveEngagementsException
from .ad_exceptions import NoPrimaryEngagementException
from .ad_exceptions import ReplicationFailedException
from .ad_exceptions import SamAccountNameNotUnique
from .ad_exceptions import UserNotFoundException
from .ad_jinja_filters import first_address_of_type
from .ad_jinja_filters import location_element
from .ad_jinja_filters import name_to_email_address
from .ad_logger import start_logging
from .ad_reader import ADParameterReader
from .ad_template_engine import INVALID
from .ad_template_engine import prepare_field_templates
from .ad_template_engine import template_powershell
from .user_names import UserNameGen
from .utils import dict_exclude
from .utils import dict_map
from .utils import dict_subset
from .utils import lower_list


logger = logging.getLogger("AdWriter")


class MODataSource(ABC):
    @abstractmethod
    def read_user(self, uuid):
        """Read a user from MO using the provided uuid.

        Throws UserNotFoundException if the user cannot be found.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            dict: A dict with the users data.
        """
        raise NotImplementedError

    @abstractmethod
    def get_email_address(self, uuid):
        """Read a users email address using the provided uuid.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            dict: A dict of email address, potentially empty.
        """
        raise NotImplementedError

    @abstractmethod
    def find_primary_engagement(self, uuid):
        """Find the primary engagement for the provided uuid user.

        Args:
            uuid: UUID for the user to find primary engagement for.

        Returns:
            tuple(string, string, string, string):
                employment_number: Identifier for the engagement.
                title: Title of the job function for the engagement
                eng_org_unit: UUID of the organisation unit for the engagement
                eng_uuid: UUID of the found engagement
        """
        raise NotImplementedError

    @abstractmethod
    def get_manager_uuid(self, mo_user, eng_uuid):
        """Get UUID of the relevant manager for the user.

        Args:
            mo_user: MO user object, as returned by read_user.
            eng_uuid: UUID of the engagement, as returned by find_primary_engagement.

        Returns:
            str: A UUID string for the manager
        """
        raise NotImplementedError

    @abstractmethod
    def get_engagement_dates(self, uuid):
        """Return all present and future engagement start dates and end dates.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            tuple[list[str], list[str]]: A tuple of lists of start and end dates.
        """
        raise NotImplementedError

    def get_engagement_endpoint_dates(self, uuid):
        """Return the earliest start- and latest end-date for the users engagements.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            tuple[str, str]: A tuple with start and end date.
        """
        start_dates, end_dates = self.get_engagement_dates(uuid)

        start_dates = map(lambda date: date if date else "1930-01-01", start_dates)
        start_date = min(start_dates, default="9999-12-31")

        end_dates = map(lambda date: date if date else "9999-12-31", end_dates)
        end_date = max(end_dates, default="1930-01-01")

        if date.fromisoformat(start_date) > date.fromisoformat(end_date):
            raise EngagementDatesError(
                "Invalid Engagement dates interval. "
                f"Start date {start_date} is greater than end date {end_date}"
            )

        return start_date, end_date

    @abstractmethod
    def get_it_systems(self, uuid):
        """Read the IT system bindings from the user using the provided uuid.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            dict of dicts: A dictionary it-system uuid to it-system dictionaries.
        """
        raise NotImplementedError


class LoraCacheSource(MODataSource):
    """LoraCache implementation of the MODataSource interface."""

    def __init__(self, lc, lc_historic, mo_rest_source):
        self.lc = lc
        self.lc_historic = lc_historic
        self.mo_rest_source = mo_rest_source

    def read_user(self, uuid):
        if uuid not in self.lc.users:
            raise UserNotFoundException()

        lc_user = self.lc.users[uuid][0]
        mo_user = {
            "uuid": uuid,
            "name": lc_user["navn"],
            "surname": lc_user["efternavn"],
            "givenname": lc_user["fornavn"],
            "nickname": lc_user["kaldenavn"],
            "nickname_givenname": lc_user["kaldenavn_fornavn"],
            "nickname_surname": lc_user["kaldenavn_efternavn"],
            "cpr_no": lc_user["cpr"],
        }
        return mo_user

    def get_email_address(self, uuid):
        mail_dict = {}
        for addr in self.lc.addresses.values():
            if addr[0]["user"] == uuid and addr[0]["scope"] == "E-mail":
                mail_dict = addr[0]
        return dict_subset(mail_dict, ["uuid", "value"])

    def find_primary_engagement(self, uuid):
        def filter_for_user(engagements):
            return filter(lambda eng: eng[0]["user"] == uuid, engagements)

        def filter_primary(engagements):
            return filter(lambda eng: eng[0]["primary_boolean"], engagements)

        user_engagements = list(filter_for_user(self.lc.engagements.values()))
        # No user engagements
        if not user_engagements:
            # But we may still have future engagements
            future_engagement = next(
                filter_for_user(self.lc_historic.engagements.values()), None
            )
            # We do not have any engagements at all
            if future_engagement is None:
                raise NoActiveEngagementsException()
            # We have future engagements, but LoraCache does not handle that.
            # Delegate to MORESTSource
            logger.info("Found future engagement")
            return self.mo_rest_source.find_primary_engagement(uuid)

        primary_engagement = next(filter_primary(user_engagements), None)
        if primary_engagement is None:
            raise NoPrimaryEngagementException("User: {}".format(uuid))

        primary_engagement = primary_engagement[0]
        employment_number = primary_engagement["user_key"]
        title = self.lc.classes[primary_engagement["job_function"]]["title"]
        eng_org_unit = primary_engagement["unit"]
        eng_uuid = primary_engagement["uuid"]
        return employment_number, title, eng_org_unit, eng_uuid

    def get_manager_uuid(self, mo_user, eng_uuid):
        def org_uuid_parent(org_uuid):
            parent_uuid = self.lc.units[org_uuid][0]["parent"]
            return parent_uuid

        def org_uuid_to_manager(org_uuid):
            org_unit = self.lc.units[org_uuid][0]
            manager_uuid = self.lc.managers[org_unit["acting_manager_uuid"]][0]["user"]
            return manager_uuid

        try:
            # Compatibility to mimic MORESTSource behaviour
            # MORESTSource does an engagement lookup in the present, using
            # the org uuid from that and fails if it doesn't find anything
            engagement = self.lc.engagements[eng_uuid][0]
            eng_org_unit = engagement["unit"]
            manager_uuid = org_uuid_to_manager(eng_org_unit)
            if manager_uuid is None:
                raise Exception("Unable to find manager")
            # We found a manager directly
            if manager_uuid != mo_user["uuid"]:
                return manager_uuid
            # Self manager, find a manager above us, if possible
            parent_uuid = org_uuid_parent(eng_org_unit)
            while manager_uuid == mo_user["uuid"]:
                if parent_uuid is None:
                    return manager_uuid
                manager_uuid = org_uuid_to_manager(parent_uuid)
                parent_uuid = org_uuid_parent(parent_uuid)
            return manager_uuid
        except KeyError:
            return None

    def get_engagement_dates(self, uuid):
        """Return all present and future engagement start dates and end dates.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            tuple[list[str], list[str]]: A tuple of lists of start and end dates.
        """
        return self.mo_rest_source.get_engagement_dates(uuid)

    def get_it_systems(self, uuid):
        user_itsystems = filter(
            lambda eng: eng["user"] == uuid,
            map(itemgetter(0), self.lc.it_connections.values()),
        )
        return {it_system["itsystem"]: it_system for it_system in user_itsystems}


class MOGraphqlSource:
    """MO Graphql."""

    def __init__(self, settings):
        self.employees = self._read_employees(settings)
        self.manager_map = self._create_manager_map(self.employees)

    def _read_employees(self, settings):
        query = gql(
            """query read_employees {
            employees(to_date: null) {
                uuid
                objects {
                engagements {
                    employee_uuid
                    uuid
                    is_primary
                    org_unit {
                    managers(inherit: true) {
                        employee_uuid
                    }
                    }
                }
                }
            }
        }
        """
        )
        with GraphQLClient(
            url=f"{settings['global']['mora.base']}/graphql",
            client_id=os.environ.get("CLIENT_ID", "dipex"),
            client_secret=os.environ["CLIENT_SECRET"],
            auth_realm=os.environ.get("AUTH_REALM", "mo"),
            auth_server=os.environ["AUTH_SERVER"],
            sync=True,
            httpx_client_kwargs={"timeout": None},
        ) as session:

            r = session.execute(query)
        return r["employees"]

    def _create_manager_map(self, employees):
        """Create mapping between employees and managers"""
        # For each employee we need to find the manager uuid which is associated by the org_unit of the primary engagement.
        # So the path is engagements - filter by primary, org_unit, manager, uuid
        # At last the result is piped into [0] to get the value and not an array.
        # https://jmespath.org/tutorial.html#pipe-expressions
        manager_uuid = jmespath.compile(
            "objects[0].engagements[?is_primary].org_unit[0].managers[0].employee_uuid | [0]"
        )
        return {e["uuid"]: manager_uuid.search(e) for e in employees}

    def get_manager_uuid(self, mo_user, eng_uuid):
        """Lookup manager for employee"""
        return self.manager_map[mo_user]


class MORESTSource(MODataSource):
    """MO REST implementation of the MODataSource interface."""

    def __init__(self, settings):
        self.helper = MoraHelper(
            hostname=settings["global"]["mora.base"], use_cache=False
        )

    def read_user(self, uuid):
        mo_user = self.helper.read_user(user_uuid=uuid)
        if "uuid" not in mo_user:
            raise UserNotFoundException()
        else:
            assert mo_user["uuid"] == uuid
        exclude_fields = ["org", "user_key"]
        mo_user = dict_exclude(mo_user, exclude_fields)
        return mo_user

    def get_email_address(self, uuid):
        mail_dict = first(self.helper.get_e_addresses(uuid, scope="EMAIL"), default={})
        return dict_subset(mail_dict, ["uuid", "value"])

    def find_primary_engagement(self, uuid):
        def filter_primary(engagements):
            return filter(lambda eng: eng["is_primary"], engagements)

        user_engagements = self.helper.read_user_engagement(
            uuid, calculate_primary=True, read_all=True, skip_past=True
        )
        if not user_engagements:
            raise NoActiveEngagementsException()

        primary_engagement = next(filter_primary(user_engagements), None)
        if primary_engagement is None:
            raise NoPrimaryEngagementException("User: {}".format(uuid))

        employment_number = primary_engagement["user_key"]
        title = primary_engagement["job_function"]["name"]
        eng_org_unit = primary_engagement["org_unit"]["uuid"]
        eng_uuid = primary_engagement["uuid"]
        return employment_number, title, eng_org_unit, eng_uuid

    def get_manager_uuid(self, mo_user, eng_uuid):
        try:
            manager = self.helper.read_engagement_manager(eng_uuid)
            manager_uuid = manager["uuid"]
            return manager_uuid
        except KeyError:
            return None

    def get_engagement_dates(self, uuid):
        """Return all present and future engagement start dates and end dates.

        Args:
            uuid: UUID for the user to lookup.

        Returns:
            tuple[list[str], list[str]]: A tuple of lists of start and end dates.
        """
        user_engagements = self.helper.read_user_engagement(
            uuid, read_all=True, skip_past=True
        )
        dates = map(itemgetter("validity"), user_engagements)
        dates = map(itemgetter("from", "to"), dates)
        unzipped = unzip(dates)
        if len(unzipped) == 0:
            return [], []
        from_dates, to_dates = unzipped
        return from_dates, to_dates

    def get_it_systems(self, uuid):
        itsystems = self.helper.get_e_itsystems(uuid)

        def to_lora_itsystem(it_system):
            return it_system["itsystem"]["uuid"], {
                "uuid": it_system["uuid"],
                "user": it_system["person"]["uuid"],
                "unit": (it_system.get("org_unit") or {"uuid": None}).get("uuid"),
                "username": it_system["user_key"],
                "itsystem": it_system["itsystem"]["uuid"],
                "from_date": it_system["validity"]["from"],
                "to_date": it_system["validity"]["to"],
            }

        return dict(map(to_lora_itsystem, itsystems))


class ADWriter(AD):
    INVALID_UNIT_ADDRESS = {
        "city": INVALID,
        "postal_code": INVALID,
        "streetname": INVALID,
    }

    def __init__(self, lc=None, lc_historic=None, **kwargs):
        super().__init__(**kwargs)
        self.settings = self.all_settings
        self.skip_occupied_names = kwargs.get("skip_occupied_names", False)

        # Setup datasource for getting MO data.
        # TODO: Create a factory instead of this hackery?
        # Default to using MORESTSource as data source
        self.datasource = MORESTSource(self.settings)
        # Use LoraCacheSource if LoraCache is provided
        if lc and lc_historic:
            self.datasource = LoraCacheSource(lc, lc_historic, self.datasource)
        # NOTE: These should be eliminated when all uses are gone
        # NOTE: Once fully utilized, tests should be able to just implement a
        #       MODataSource for all their mocking needs.
        self.lc = lc
        self.lc_historic = lc_historic
        self.helper = MoraHelper(
            hostname=self.settings["global"]["mora.base"], use_cache=False
        )
        # Read manager information with graphql
        graphql = MOGraphqlSource(self.settings)
        # overwrite method no matter what datasource.
        self.datasource.get_manager_uuid = graphql.get_manager_uuid

        self._init_name_creator()

        self._environment = self._get_jinja_environment()

    def _init_name_creator(self):
        self.name_creator = UserNameGen.get_implementation()
        if not self.skip_occupied_names:
            logger.info("Reading occupied names")
            self.name_creator.load_occupied_names()
        logger.info("Done reading occupied names")

    def _get_write_setting(self):
        if not self.all_settings["primary_write"]:
            msg = "Trying to enable write access with broken settings."
            logger.error(msg)
            raise Exception(msg)
        return self.all_settings["primary_write"]

    def _wait_for_replication(self, sam):
        t_start = time.time()
        logger.debug("Wait for replication of {}".format(sam))
        if not self.all_settings["global"]["servers"]:
            logger.info("No server infomation, falling back to waiting")
            time.sleep(15)
        else:
            # TODO, read from all AD servers and see when user is available
            replication_finished = False
            while not replication_finished:
                if time.time() - t_start > 60:
                    logger.error("Replication error")
                    raise ReplicationFailedException()

                for server in self.all_settings["global"]["servers"]:
                    user = self.get_from_ad(user=sam, server=server)
                    logger.debug("Testing {}, found: {}".format(server, len(user)))
                    if user:
                        logger.debug("Found successfully")
                        replication_finished = True
                    else:
                        logger.debug("Did not find")
                        replication_finished = False
                        time.sleep(0.25)
                        break
        logger.info("replication_finished: {}s".format(time.time() - t_start))

    def _read_user(self, uuid):
        return self.datasource.read_user(uuid)

    def _find_unit_info(self, eng_org_unit):
        # TODO: Convert to datasource
        write_settings = self._get_write_setting()

        level2orgunit = "Ingen"
        unit_info = {}
        if self.lc:
            unit_name = self.lc.units[eng_org_unit][0]["name"]
            unit_user_key = self.lc.units[eng_org_unit][0]["user_key"]
            location = self.lc.units[eng_org_unit][0]["location"]

            # We initialize parent as the unit itself to ensure to catch if
            # a person is engaged directly in a level2org
            parent_uuid = self.lc.units[eng_org_unit][0]["uuid"]
            while parent_uuid is not None:
                parent_unit = self.lc.units[parent_uuid][0]
                if write_settings["level2orgunit_type"] in (
                    parent_unit["unit_type"],
                    parent_unit["level"],
                ):
                    level2orgunit = parent_unit["name"]
                parent_uuid = parent_unit["parent"]
        else:
            mo_unit_info = self.helper.read_ou(eng_org_unit)
            unit_name = mo_unit_info["name"]
            unit_user_key = mo_unit_info["user_key"]

            location = ""
            current_unit = mo_unit_info
            while current_unit:
                location = current_unit["name"] + "\\" + location
                current_type = current_unit["org_unit_type"]
                current_level = current_unit["org_unit_level"]
                if current_level is None:
                    current_level = {"uuid": None}
                if write_settings["level2orgunit_type"] in (
                    current_type["uuid"],
                    current_level["uuid"],
                ):
                    level2orgunit = current_unit["name"]
                current_unit = current_unit["parent"]
            location = location[:-1]

        unit_info = {
            "name": unit_name,
            "user_key": unit_user_key,
            "location": location,
            "level2orgunit": level2orgunit,
        }
        return unit_info

    def _read_user_addresses(self, eng_org_unit):
        # TODO: Convert to datasource
        addresses = {}
        if self.lc:
            email = []
            postal = {}
            for addr in self.lc.addresses.values():
                if addr[0]["unit"] == eng_org_unit:
                    if addr[0]["scope"] == "DAR":
                        postal = {"Adresse": addr[0]["value"]}
                    if addr[0]["scope"] == "E-mail":
                        visibility = addr[0]["visibility"]
                        visibility_class = None
                        if visibility is not None:
                            visibility_class = self.lc.classes[visibility]
                        email.append(
                            {"visibility": visibility_class, "value": addr[0]["value"]}
                        )
        else:
            email = self.helper.read_ou_address(
                eng_org_unit, scope="EMAIL", return_all=True
            )
            postal = self.helper.read_ou_address(
                eng_org_unit, scope="DAR", return_all=False
            )

        unit_secure_email = None
        unit_public_email = None
        for mail in email:
            if mail["visibility"] is None:
                # If visibility is not set, we assume it is non-public.
                unit_secure_email = mail["value"]
            else:
                if mail["visibility"]["scope"] == "PUBLIC":
                    unit_public_email = mail["value"]
                if mail["visibility"]["scope"] == "SECRET":
                    unit_secure_email = mail["value"]

        addresses = {
            "unit_secure_email": unit_secure_email,
            "unit_public_email": unit_public_email,
            "postal": postal,
        }
        return addresses

    def read_ad_information_from_mo(self, uuid, read_manager=True, ad_dump=None):
        """
        Retrive the necessary information from MO to contruct a new AD user.
        The final information object should of this type, notice that end-date
        is not necessarily for the current primary engagement, but the end-date
        of the longest running currently known primary engagement:
        mo_values = {
            'name': ('Martin Lee', 'Gore'),
            'employment_number': '101',
            'uuid': '7ccbd9aa-gd60-4fa1-4571-0e6f41f6ebc0',
            'end_date': 2089-11-11,
            'cpr': '1122334455',
            'title': 'Musiker',
            'location': 'Viborg Kommune\\Forvalting\\Enhed\\',
            'level2orgunit: 'Beskæftigelse, Økonomi & Personale',
            'manager_sam': 'DMILL'
        }
        """
        logger.info("Read information for {}".format(uuid))
        try:
            (
                employment_number,
                title,
                eng_org_unit,
                eng_uuid,
            ) = self.datasource.find_primary_engagement(uuid)
        except NoActiveEngagementsException:
            logger.info("No active engagements found")
            return None

        def split_addresses(addresses):
            try:
                postal = addresses["postal"]["Adresse"]
            except KeyError:
                return ADWriter.INVALID_UNIT_ADDRESS
            else:
                try:
                    postal_code = re.findall("[0-9]{4}", postal)[0]
                    city_pos = postal.find(postal_code) + 5
                    city = postal[city_pos:]
                    streetname = postal[: city_pos - 7]
                except (IndexError, TypeError):
                    logger.error("Unable to read adresse from MO (no access to DAR?)")
                    return ADWriter.INVALID_UNIT_ADDRESS
                else:
                    return {
                        "postal_code": postal_code,
                        "city": city,
                        "streetname": streetname,
                    }

        def read_manager_uuid(mo_user, eng_uuid):
            manager_uuid = self.datasource.get_manager_uuid(mo_user, eng_uuid)
            if manager_uuid is None:
                logger.info("No managers found")
            return manager_uuid

        def read_manager_mail(manager_uuid):
            manager_mail_dict = self.datasource.get_email_address(manager_uuid)
            if manager_mail_dict:
                return manager_mail_dict["value"]
            return None

        def read_manager_sam(manager_cpr):
            try:
                manager_ad_user = self._find_ad_user(cpr=manager_cpr, ad_dump=ad_dump)
            except CprNotFoundInADException:
                logger.info("manager not found by cpr lookup")
            except CprNotNotUnique:
                logger.info("multiple managers found by cpr lookup")
            else:
                return self._get_sam_from_ad_values(manager_ad_user)
            return None

        # NOTE: Underscore fields should not be read
        mo_values: LazyDict = LazyDict(
            {
                # Raw information
                "uuid": uuid,
                # Engagement information
                "employment_number": employment_number,
                "title": title,
                "unit_uuid": eng_org_unit,
                "_eng_uuid": eng_uuid,
                "_dates": LazyEvalDerived(
                    lambda uuid: self.datasource.get_engagement_endpoint_dates(uuid)
                ),
                "start_date": LazyEvalDerived(lambda _dates: _dates[0]),
                "end_date": LazyEvalDerived(lambda _dates: _dates[1]),
                # Lazy MO User and associated fields
                "_mo_user": LazyEvalDerived(lambda uuid: self._read_user(uuid)),
                "name": LazyEvalDerived(
                    lambda _mo_user: (_mo_user["givenname"], _mo_user["surname"])
                ),
                "full_name": LazyEvalDerived(
                    lambda name: "{} {}".format(*name).strip()
                ),
                "nickname": LazyEvalDerived(
                    lambda _mo_user: (
                        _mo_user["nickname_givenname"],
                        _mo_user["nickname_surname"],
                    )
                ),
                "full_nickname": LazyEvalDerived(
                    lambda nickname: "{} {}".format(*nickname).strip()
                ),
                "cpr": LazyEvalDerived(lambda _mo_user: _mo_user["cpr_no"]),
                # Lazy Unit and associated fields
                "_unit": LazyEvalDerived(
                    lambda unit_uuid: self._find_unit_info(unit_uuid)
                ),
                "unit": LazyEvalDerived(lambda _unit: _unit["name"]),
                "unit_user_key": LazyEvalDerived(lambda _unit: _unit["user_key"]),
                "location": LazyEvalDerived(lambda _unit: _unit["location"]),
                "level2orgunit": LazyEvalDerived(lambda _unit: _unit["level2orgunit"]),
                # Lazy addresses and associated fields
                "_addresses": LazyEvalDerived(
                    lambda unit_uuid: self._read_user_addresses(unit_uuid)
                ),
                "_parsed_addresses": LazyEvalDerived(
                    lambda _addresses: split_addresses(_addresses)
                ),
                "unit_postal_code": LazyEvalDerived(
                    lambda _parsed_addresses: _parsed_addresses["postal_code"]
                ),
                "unit_city": LazyEvalDerived(
                    lambda _parsed_addresses: _parsed_addresses["city"]
                ),
                "unit_streetname": LazyEvalDerived(
                    lambda _parsed_addresses: _parsed_addresses["streetname"]
                ),
                "unit_public_email": LazyEvalDerived(
                    lambda _addresses: _addresses["unit_public_email"]
                ),
                "unit_secure_email": LazyEvalDerived(
                    lambda _addresses: _addresses["unit_secure_email"]
                ),
                # Manager stuff
                "_manager_uuid": LazyEval(
                    lambda key, dictionary: (
                        read_manager_uuid(
                            dictionary["_mo_user"], dictionary["_eng_uuid"]
                        )
                        if read_manager
                        else None
                    )
                ),
                "_manager_mo_user": LazyEvalDerived(
                    lambda _manager_uuid: self._read_user(_manager_uuid)
                    if _manager_uuid
                    else {}
                ),
                "manager_name": LazyEvalDerived(
                    lambda _manager_mo_user: _manager_mo_user.get("name")
                ),
                "manager_cpr": LazyEvalDerived(
                    lambda _manager_mo_user: _manager_mo_user.get("cpr_no")
                ),
                "manager_mail": LazyEvalDerived(
                    lambda _manager_uuid: read_manager_mail(_manager_uuid)
                    if _manager_uuid
                    else None
                ),
                "manager_sam": LazyEvalDerived(
                    lambda manager_cpr: read_manager_sam(manager_cpr)
                    if manager_cpr
                    else None
                ),
                "read_manager": LazyEvalDerived(
                    lambda _manager_uuid: bool(_manager_uuid)
                ),
                # Employee addresses
                "employee_addresses": LazyEvalDerived(
                    lambda uuid: self.helper.get_e_addresses(uuid)
                ),
                # IT systems
                "it_systems": LazyEvalDerived(
                    lambda uuid: self.datasource.get_it_systems(uuid)
                ),
            }
        )
        return mo_values

    def add_manager_to_user(self, user_sam, manager_sam):
        """
        Mark an existing AD user as manager for an existing AD user.
        :param user_sam: SamAccountName for the employee.
        :param manager_sam: SamAccountName for the manager.
        """
        ps_script = self._get_add_manager_command(user_sam, manager_sam)
        response = self._run_ps_script(ps_script)
        return response is {}

    def _get_add_manager_command(self, user_sam, manager_sam):
        format_rules = {"user_sam": user_sam, "manager_sam": manager_sam}
        ps_script = self._build_ps(ad_templates.add_manager_template, format_rules)
        return ps_script

    def _rename_ad_user(self, user_sam, new_name):
        logger.info("Rename user: %s", user_sam)
        ps_script = self._get_rename_ad_user_command(user_sam, new_name)
        logger.debug("Rename user, ps_script: {}".format(ps_script))
        response = self._run_ps_script(ps_script)
        logger.debug("Response from sync: {}".format(response))
        logger.debug("Wait for replication")
        # Todo: In principle we should ask all DCs, bu this will happen
        # very rarely, performance is not of great importance
        time.sleep(10)

    def _get_rename_ad_user_command(self, user_sam, new_name):
        # Todo: This code is a duplicate of code found elsewhere
        # Todo: This code is buggy - it uses a "stringified tuple" as the new
        # AD username. E.g. the AD user is renamed to
        # `"(\"Firstname\", \"Lastname\")"`.
        rename_user_template = ad_templates.rename_user_template
        rename_user_string = rename_user_template.format(
            user_sam=user_sam,
            new_name=new_name,
        )
        rename_user_string = self.remove_redundant(rename_user_string)
        server_string = ""
        if self.all_settings["global"].get("servers") is not None:
            server_string = " -Server {} ".format(
                random.choice(self.all_settings["global"]["servers"])
            )
        ps_script = self._build_user_credential() + rename_user_string + server_string
        return ps_script

    def _compare_fields(self, ad_field, value, ad_user):
        mismatch = {}
        ad_field_value = ad_user.get(ad_field)

        # Some AD fields contain one-element lists, rather than the usual strings,
        # numbers or UUIDs.
        # In such cases, we "unpack" the single-element list before comparing it to the
        # corresponding MO value - otherwise the comparison will not work as expected.
        if isinstance(ad_field_value, list) and len(ad_field_value) == 1:
            ad_field_value = ad_field_value[0]

        # The "MO value" in `value` actually comes from a call to
        # `_render_field_template`, which may produce the string `"None"` when it
        # encounters a `None`.
        if value == "None":
            value = None

        # We also consider the reverse situation where the AD field contains the string
        # "None" to indicate an empty value.
        if ad_field_value == "None":
            ad_field_value = None

        # Do the actual comparison
        if value == INVALID:
            msg = "%r: MO value is INVALID, not changing AD value %r"
            logger.info(msg, ad_field, ad_field_value)
        elif ad_field_value != value:
            msg = "%r: AD value %r does not match MO value %r"
            logger.info(msg, ad_field, ad_field_value, value)
            mismatch = {ad_field: (ad_field_value, value)}
        else:
            msg = "%r: AD value %r already matches MO value %r"
            logger.debug(msg, ad_field, ad_field_value, value)

        return mismatch

    def _get_jinja_environment(self):
        environment = Environment(undefined=StrictUndefined)

        # Add custom filters
        environment.filters["first_address_of_type"] = first_address_of_type
        environment.filters["name_to_email_address"] = name_to_email_address
        environment.filters["location_element"] = location_element

        @lru_cache
        def get_all_ad_emails():
            reader = ADParameterReader(all_settings=self.all_settings)
            return reader.get_all_email_values()

        # Add globally available vars
        environment.globals["_upn_end"] = self.settings["primary_write"]["upn_end"]
        environment.globals["_get_all_ad_emails"] = get_all_ad_emails

        return environment

    def _render_field_template(self, context, template):
        env = self._environment.overlay(undefined=Undefined)
        template = env.from_string(template.strip('"'))
        return template.render(**context)

    def _preview_create_command(self, mo_uuid, ad_dump=None, create_manager=True):
        mo_values = self.read_ad_information_from_mo(
            mo_uuid,
            ad_dump=ad_dump,
            read_manager=create_manager,
        )
        sam_account_name = self._get_create_user_sam_account_name(
            mo_values, dry_run=True
        )
        create_cmd = self._get_create_user_command(mo_values, sam_account_name)
        add_manager_cmd = self._get_add_manager_command(
            sam_account_name, mo_values["manager_sam"]
        )
        return create_cmd, add_manager_cmd

    def _preview_sync_command(self, mo_uuid, user_sam, ad_dump=None, sync_manager=True):
        mo_values = self.read_ad_information_from_mo(
            mo_uuid, ad_dump=ad_dump, read_manager=sync_manager
        )

        try:
            ad_values = self._find_ad_user(mo_values["cpr"], ad_dump=ad_dump)
        except CprNotFoundInADException:
            ad_values = {}
        else:
            user_sam = self._get_sam_from_ad_values(ad_values)

        sync_cmd = self._get_sync_user_command(ad_values, mo_values, user_sam)
        rename_cmd = ""
        rename_cmd_target = ""

        try:
            mismatch = self._sync_compare(mo_values, ad_dump)
        except CprNotFoundInADException:
            # We might be running against an AD where we have not yet written any actual
            # users. In that case, we will not find the user by CPR. But we still want
            # to see the rename command that would be issued.
            logger.info("Previewing 'rename' command for nonexistent AD user")
            rename_cmd = self._get_rename_ad_user_command(user_sam, "<new name>")
            rename_cmd_target = "<nonexistent AD user>"
        else:
            # We found an actual AD user by CPR
            if "name" in mismatch:
                # A rename command is necessary, as the new username differs from the
                # current username in AD.
                logger.info("Previewing 'rename' command for existing AD user")
                new_name = mismatch["name"][1]
                rename_cmd = self._get_rename_ad_user_command(user_sam, new_name)
                rename_cmd_target = mismatch["name"][0]  # = previous username

        return sync_cmd, rename_cmd, rename_cmd_target

    def _sync_compare(self, mo_values, ad_dump):
        ad_user = self._find_ad_user(mo_values["cpr"], ad_dump=ad_dump)
        user_sam = self._get_sam_from_ad_values(ad_user)

        # TODO: Why is this not generated along with all other info in mo_values?
        mo_values["name_sam"] = "{} - {}".format(mo_values["full_name"], user_sam)

        fields = prepare_field_templates("Set-ADUser", settings=self.all_settings)

        def to_lower(string):
            return string.lower()

        ad_user = dict_map(ad_user, key_func=to_lower)
        fields = dict_map(fields, key_func=to_lower)

        never_compare = lower_list(["Credential", "Manager"])
        fields = dict_exclude(fields, never_compare)

        context = {
            "ad_values": ad_user,
            "mo_values": mo_values,
            "user_sam": user_sam,
        }

        # Build context and render template to get comparision value
        # NOTE: This results in rendering the template twice, once here and
        #       once inside the powershell render call.
        #       We should probably restructure this, such that we only render
        #       the template once, potentially rendering a dict of results.
        # TODO: Make the above mentioned change.
        fields = dict_map(
            fields,
            value_func=partial(self._render_field_template, context),
        )
        mismatch = {}
        for ad_field, rendered_value in fields.items():
            mismatch.update(self._compare_fields(ad_field, rendered_value, ad_user))

        if mo_values.get("manager_cpr"):
            try:
                manager_ad_user = self._find_ad_user(
                    mo_values["manager_cpr"], ad_dump=ad_dump
                )
            except CprNotFoundInADException:
                logger.warning(
                    "could not find manager by cpr %r", mo_values.get("manager_cpr")
                )
            else:
                manager_distinguished_name = manager_ad_user["DistinguishedName"]
                if ad_user["manager"] != manager_distinguished_name:
                    mismatch["manager"] = (
                        ad_user["manager"],
                        manager_distinguished_name,
                    )
                    logger.info("Manager should be updated")

        return mismatch

    def sync_user(self, mo_uuid, ad_dump=None, sync_manager=True):
        """
        Sync MO information into AD
        """
        mo_values = self.read_ad_information_from_mo(
            mo_uuid, ad_dump=ad_dump, read_manager=sync_manager
        )

        if mo_values is None:
            return (False, "No active engagments")

        ad_values = self._find_ad_user(mo_values["cpr"], ad_dump=ad_dump)
        user_sam = self._get_sam_from_ad_values(ad_values)

        if ad_dump is None:
            # TODO: We could also add the compare logic here,
            # but the benefit will be max 40%
            mismatch = {"force re-sync": "yes", "manager": "yes"}
        else:
            mismatch = self._sync_compare(mo_values, ad_dump)

        logger.debug("Sync compare: {}".format(mismatch))

        if "name" in mismatch:
            response = self._rename_ad_user(user_sam, mismatch["name"][1])
            del mismatch["name"]

        if not mismatch and ("sync_timestamp" not in str(self.all_settings)):
            # If "sync_timestamp" is in settings we assume the intent is to always write a timestamp.
            logger.info("Nothing to edit")
            return (True, "Nothing to edit", mo_values["read_manager"])

        logger.info("Sync compare: {}".format(mismatch))

        ps_script = self._get_sync_user_command(ad_values, mo_values, user_sam)
        logger.debug("Sync user, ps_script: {}".format(ps_script))

        response = self._run_ps_script(ps_script)
        logger.debug("Response from sync: {}".format(response))

        if sync_manager and "manager" in mismatch:
            logger.info("Add manager")
            self.add_manager_to_user(
                user_sam=user_sam, manager_sam=mo_values["manager_sam"]
            )

        return (True, "Sync completed", mo_values["read_manager"])

    def _get_sync_user_command(self, ad_values, mo_values, user_sam):
        edit_user_string = template_powershell(
            cmd="Set-ADUser",
            context={
                "ad_values": ad_values,
                "mo_values": mo_values,
                "user_sam": user_sam,
                "sync_timestamp": str(datetime.now()),
            },
            settings=self.all_settings,
            environment=self._environment,
        )
        edit_user_string = self.remove_redundant(edit_user_string)

        server_string = ""
        if self.all_settings["global"].get("servers") is not None:
            server_string = " -Server {} ".format(
                random.choice(self.all_settings["global"]["servers"])
            )

        ps_script = self._build_user_credential() + edit_user_string + server_string
        return ps_script

    def create_user(self, mo_uuid, create_manager, dry_run=False):
        """
        Create an AD user
        :param mo_uuid: uuid for the MO user we want to add to AD.
        :param create_manager: If True, an AD link will be added between the user
        object and the AD object of the users manager.
        :param dry_run: Not yet implemented. Should return whether the user is
        expected to be able to be created in AD and the expected SamAccountName.
        :return: The generated SamAccountName for the new user
        """
        mo_values = self.read_ad_information_from_mo(mo_uuid, create_manager)
        if mo_values is None:
            logger.error("Trying to create user with no engagements")
            raise NoPrimaryEngagementException

        sam_account_name = self._get_create_user_sam_account_name(
            mo_values, dry_run=dry_run
        )

        self._check_if_ad_user_exists(sam_account_name, mo_values["cpr"])

        ps_script = self._get_create_user_command(mo_values, sam_account_name)

        response = self._run_ps_script(ps_script)
        if not response == {}:
            msg = "Create user failed, message: {}".format(response)
            logger.error(msg)
            return (False, msg)

        if create_manager:
            self._wait_for_replication(sam_account_name)
            msg = "Add {} as manager for {}".format(
                mo_values["manager_sam"], sam_account_name
            )
            print(msg)
            logger.info(msg)
            self.add_manager_to_user(
                user_sam=sam_account_name, manager_sam=mo_values["manager_sam"]
            )

        return (True, sam_account_name)

    def _get_create_user_sam_account_name(self, mo_values, dry_run=False):
        all_names = mo_values["name"][0].split(" ") + [mo_values["name"][1]]
        return self.name_creator.create_username(all_names, dry_run=dry_run)

    def _get_create_user_command(self, mo_values, sam_account_name):
        create_user_string = template_powershell(
            context={
                "ad_values": {},
                "mo_values": mo_values,
                "user_sam": sam_account_name,
                "sync_timestamp": str(datetime.now()),
            },
            settings=self.all_settings,
            environment=self._environment,
        )
        create_user_string = self.remove_redundant(create_user_string)

        # Should this go to self._ps_boiler_plate()?
        server_string = ""
        if self.all_settings["global"].get("servers"):
            server_string = " -Server {} ".format(
                random.choice(self.all_settings["global"]["servers"])
            )

        ps_script = (
            self._build_user_credential()
            + create_user_string
            + server_string
            + self._get_new_ad_user_path_argument()
        )

        return ps_script

    def _get_new_ad_user_path_argument(self):
        primary = self._get_setting()
        primary_write = self._get_write_setting()
        path = primary_write.get("new_ad_user_path", primary["search_base"])
        path_argument = f' -Path "{path}"'
        return path_argument

    def _check_if_ad_user_exists(self, sam_account_name, cpr):
        existing_sam = self.get_from_ad(user=sam_account_name)
        existing_cpr = self.get_from_ad(cpr=cpr)
        if existing_sam:
            logger.error("SamAccount already in use: {}".format(sam_account_name))
            raise SamAccountNameNotUnique(sam_account_name)
        if existing_cpr:
            logger.error("cpr already in use: {}".format(cpr))
            raise CprNotNotUnique(cpr)

    def add_ad_to_user_it_systems(self, username):
        # TODO: We need a function to write the SamAccount to the user's
        # IT-systems. This is most likely most elegantly done by importing
        # the AD->MO sync tool
        pass

    def set_user_password(self, username, password):
        """
        Set a password for a user.
        :param username: SamAccountName for the user.
        :param password: The password to assign to the user.
        :return: True if success, otherwise False
        """

        format_rules = {"username": username, "password": password}
        ps_script = self._build_ps(ad_templates.set_password_template, format_rules)
        response = self._run_ps_script(ps_script)
        if not response:
            return (True, "Password updated")
        else:
            msg = "Failed to set password!: {}".format(response)
            logger.error(msg)
            return (False, msg)

    def enable_user(self, username, enable=True):
        """
        Enable or disable an AD account.
        :param username: SamAccountName of the account to be enabled or disabled
        :param enable: If True enable account, if False, disbale account
        """

        logger.info("Enable account: {}".format(enable))
        format_rules = {"username": username}
        if enable:
            ps_script = self._build_ps(ad_templates.enable_user_template, format_rules)
        else:
            ps_script = self._build_ps(ad_templates.disable_user_template, format_rules)

        response = self._run_ps_script(ps_script)
        if not response:
            return (True, "Account enabled or disabled")
        else:
            msg = "Failed to update account!: {}".format(response)
            logger.error(msg)
            return (False, msg)

    def delete_user(self, username):
        """
        Delete an AD account. Only to be used for service purpose, actual
        AD integration should never delete a user, but only mark it for
        deletetion.
        :param username: SamAccountName of the account to be deleted
        """
        format_rules = {"username": username}
        ps_script = self._build_ps(
            ad_templates.delete_user_template, format_rules=format_rules
        )

        response = self._run_ps_script(ps_script)
        # TODO: Should we make a read to confirm the user is gone?
        if not response:
            return (True, "User deleted")
        else:
            logger.error("Failed to delete account!: {}".format(response))
            return (False, "Failed to delete")


@click.command()
@optgroup.group("Action", cls=RequiredMutuallyExclusiveOptionGroup)
@optgroup.option(
    "--create-user-with-manager",
    help="Create a new user in AD, also assign a manager",
)
@optgroup.option(
    "--create-user",
    help="Create a new user in AD, do not assign a manager",
)
@optgroup.option(
    "--sync-user",
    help="Sync relevant fields from MO to AD",
)
@optgroup.option(
    "--mo-values",
    type=click.UUID,
    help="Show mo-values for the user",
)
@optgroup.option("--delete-user")
@optgroup.option("--read-ad-information")
@optgroup.option("--add-manager-to-user", nargs=2, type=str)
@click.option("--ignore-occupied-names", is_flag=True, default=False)
def cli(**args):
    """
    Command line interface for the AD writer class.
    """

    ad_writer = ADWriter(skip_occupied_names=args["ignore_occupied_names"])

    if args.get("create_user_with_manager"):
        print("Create_user_with_manager:")
        status = ad_writer.create_user(
            args["create_user_with_manager"], create_manager=True
        )
        # TODO: execute custom script? Or should this be done in
        # two steps.
        print(status[1])

    if args.get("create_user"):
        print("Create user, no link to manager:")
        status = ad_writer.create_user(args["create_user"], create_manager=False)
        print(status[1])

    if args.get("sync_user"):
        print("Sync MO fields to AD")
        status = ad_writer.sync_user(args["sync_user"])
        print(status[1])

    if args.get("delete_user"):
        print("Deleting user:")
        status = ad_writer.delete_user(args["delete_user"])
        print(status[1])

    if args.get("read_ad_information"):
        print("AD information on user:")
        sam = args["read_ad_information"]
        user = ad_writer.get_from_ad(user=sam)
        if not user:
            print("User not found")
        else:
            for key, value in sorted(user[0].items()):
                print("{}: {}".format(key, value))

    if args.get("add_manager_to_user"):
        manager, user = args["add_manager_to_user"]
        print("{} is now set as manager for {}".format(manager, user))
        ad_writer.add_manager_to_user(manager_sam=manager, user_sam=user)

    if args.get("mo_values"):
        mo_values = ad_writer.read_ad_information_from_mo(str(args["mo_values"]))
        print(json.dumps(dict(mo_values.items()), indent=4))


if __name__ == "__main__":
    start_logging("ad_writer.log")
    cli()
