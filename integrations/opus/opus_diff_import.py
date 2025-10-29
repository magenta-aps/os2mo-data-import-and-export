import asyncio
import logging
import sys
from datetime import datetime
from datetime import timedelta
from operator import itemgetter
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

import requests
from fastramqpi.ra_utils.load_settings import load_settings
from fastramqpi.ra_utils.tqdm_wrapper import tqdm
from fastramqpi.raclients.graph.client import GraphQLClient
from gql import gql
from more_itertools import one
from more_itertools import only
from os2mo_helpers.mora_helpers import MoraHelper
from requests import Session

import constants
from integrations import dawa_helper
from integrations.ad_integration import ad_reader
from integrations.opus import opus_helpers
from integrations.opus import payloads
from integrations.opus.ad import LdapADGUIDReader
from integrations.opus.opus_exceptions import RunDBInitException
from integrations.opus.opus_exceptions import UnknownOpusUnit

logger = logging.getLogger("opusDiff")

LOG_LEVEL = logging.DEBUG

logger = logging.getLogger("opusImport")

for name in logging.root.manager.loggerDict:
    if name in (
        "opusImport",
        "opusHelper",
        "opusDiff",
        "moImporterMoraTypes",
        "moImporterMoxTypes",
        "moImporterUtilities",
        "moImporterHelpers",
        "ADReader",
    ):
        logging.getLogger(name).setLevel(LOG_LEVEL)
    else:
        logging.getLogger(name).setLevel(logging.ERROR)

logging.basicConfig(
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
    level=LOG_LEVEL,
    stream=sys.stdout,
)
UNIT_ADDRESS_CHECKS = {
    "seNr": constants.addresses_unit_se,
    "cvrNr": constants.addresses_unit_cvr,
    "eanNr": constants.addresses_unit_ean,
    "pNr": constants.addresses_unit_pnr,
    "phoneNumber": constants.addresses_unit_phoneNumber,
    "dar": constants.addresses_unit_dar,
}

EMPLOYEE_ADDRESS_CHECKS = {
    "phone": constants.addresses_employee_phone,
    "email": constants.addresses_employee_email,
    "dar": constants.addresses_employee_dar,
}
predefined_scopes = {
    constants.addresses_employee_dar: "DAR",
    constants.addresses_employee_phone: "PHONE",
    constants.addresses_employee_email: "EMAIL",
    constants.addresses_unit_dar: "DAR",
    constants.addresses_unit_phoneNumber: "PHONE",
    constants.addresses_unit_ean: "EAN",
    constants.addresses_unit_cvr: "TEXT",
    constants.addresses_unit_pnr: "TEXT",
    constants.addresses_unit_se: "TEXT",
}

MUTATION_DELETE_ENGAGEMENT = gql(
    """
    mutation DeleteEngagement($uuid: UUID!) {
      engagement_delete(uuid: $uuid) {
        uuid
      }
    }
    """
)

QUERY_FIND_ENGAGEMENT_PRESENT = gql(
    """
        query FindEngagement($user_key: String!) {
          engagements(filter: { user_keys: [$user_key]}) {
            objects {
              uuid
            }
          }
        }
        """
)
QUERY_FIND_ENGAGEMENT = gql(
    """
        query FindEngagement($user_key: String!) {
          engagements(filter: { user_keys: [$user_key], to_date: null, from_date: null}) {
            objects {
              uuid
            }
          }
        }
        """
)

QUERY_FIND_MANAGER_PRESENT = gql(
    """
        query FindManager($user_key: String!) {
          managers(filter: { user_keys: [$user_key]}) {
            objects {
              uuid
            }
          }
        }
        """
)
QUERY_FIND_MANAGER = gql(
    """
        query FindManager($user_key: String!) {
          managers(filter: { user_keys: [$user_key], to_date: null, from_date: null}) {
            objects {
              uuid
            }
          }
        }
        """
)


class MOPostDryRun:
    def __init__(self, endpoint, payload) -> None:
        logger.info(f"dry-run: {endpoint=} {payload=}")
        self.status_code = 201 if "create" in endpoint else 200
        self.text = "Dry-run"

    def raise_for_status(self):
        pass

    def json(self):
        return {}


class OpusDiffImport(object):
    dar_cache: dict[str, UUID | None] = {}

    def __init__(
        self,
        xml_date,
        ad_reader,
        filter_ids={},
        dry_run: bool = False,
    ):
        logger.info("Opus diff importer __init__ started")
        self.xml_date = xml_date
        self.ad_reader = ad_reader

        self.settings = load_settings()
        self.filter_ids = filter_ids or self.settings.get(
            "integrations.opus.units.filter_ids", []
        )
        self.dry_run = dry_run

        self.session = Session()
        self.helper = self._get_mora_helper(
            hostname=self.settings["mora.base"], use_cache=False
        )
        self.gql_client = self._setup_gql_client()
        if dry_run:
            self.helper._mo_post = MOPostDryRun
        try:
            self.org_uuid = self.helper.read_organisation()
        except KeyError:
            msg = "No root organisation in MO"
            logger.warning(msg)
            print(msg)
            return
        except requests.exceptions.RequestException as e:
            logger.error(e)
            print(e)
            exit()

        it_systems = self.helper.read_it_systems()
        self.it_systems = dict(map(itemgetter("name", "uuid"), it_systems))

        logger.info("__init__ done, now ready for import")

    def _setup_gql_client(self) -> GraphQLClient:
        # TODO: pydantic settings!
        return GraphQLClient(
            url=f"{self.settings['mora.base']}/graphql/v22",
            client_id=self.settings["crontab.CLIENT_ID"],
            client_secret=self.settings["crontab.CLIENT_SECRET"],
            auth_realm="mo",
            auth_server=self.settings["crontab.AUTH_SERVER"],
            httpx_client_kwargs={"timeout": 300},
            execute_timeout=300,
            sync=True,
        )

    def ensure_class_in_facet(self, *args, **kwargs):
        """Helper function to call ensure_class_in_facet from morahelpers with owner"""
        return self.helper.ensure_class_in_facet(
            *args, owner=opus_helpers.find_opus_root_unit_uuid(), **kwargs
        )

    def _find_classes(self, facet):
        class_types = self.helper.read_classes_in_facet(facet)
        types_dict = {}
        facet = class_types[1]
        for class_type in class_types[0]:
            types_dict[class_type["user_key"]] = class_type["uuid"]
        return types_dict, facet

    def _get_mora_helper(self, hostname="localhost:5000", use_cache=False):
        return MoraHelper(hostname=self.settings["mora.base"], use_cache=False)

    async def find_address(self, address_string: str, zip_code: str) -> str | None:
        address = (address_string, zip_code)
        if address not in self.dar_cache:
            dar_uuid = await dawa_helper.dawa_lookup(*address)
            self.dar_cache[address] = dar_uuid
        return self.dar_cache[address]

    # This exact function also exists in sd_changed_at
    def _assert(self, response):
        """Check response is as expected"""
        assert response.status_code in (200, 400, 404)
        if response.status_code == 400:
            # Check actual response
            assert "not give raise to a new registration" in response.text, (
                response.text
            )
            logger.info("Requst had no effect")
        return None

    def _find_engagement(self, opus_id: int, present=False) -> UUID | None:
        q = QUERY_FIND_ENGAGEMENT_PRESENT if present else QUERY_FIND_ENGAGEMENT

        res = self.gql_client.execute(q, variable_values={"user_key": str(opus_id)})
        try:
            return one(res["engagements"]["objects"])["uuid"]
        except ValueError:
            return None

    def _find_manager_role(self, opus_id: int, present=False):
        q = QUERY_FIND_MANAGER_PRESENT if present else QUERY_FIND_MANAGER
        res = self.gql_client.execute(q, variable_values={"user_key": str(opus_id)})
        try:
            return one(res["managers"]["objects"])["uuid"]
        except ValueError:
            return None

    def validity(self, employee, edit=False):
        """
        Calculates a validity object from en employee object.
        :param employee: An Opus employee object.
        :param edit: If True from will be current dump date, if true
        from will be taken from emploee object.
        :return: A valid MO valididty payload
        """
        to_date = employee["leaveDate"]
        # if to_date is None: # This can most likely be removed
        #     to_datetime = datetime.strptime('9999-12-31', '%Y-%m-%d')
        # else:
        #     to_datetime = datetime.strptime(to_date, '%Y-%m-%d')

        from_date = employee["entryDate"]
        if (from_date is None) or edit:
            from_date = self.xml_date.strftime("%Y-%m-%d")
        validity = {"from": from_date, "to": to_date}
        return validity

    def _condense_employee_mo_addresses(self, mo_uuid):
        """
        Read all addresses from MO an return as a simple dict
        """
        # Unfortunately, mora-helper currently does not read all addresses
        user_addresses = self.helper._mo_lookup(mo_uuid, "e/{}/details/address")
        address_dict = {}  # Condensate of all MO addresses for the employee
        if not isinstance(user_addresses, list):
            # In case the request to mo fails we assume no addresses in MO.
            # This has happend when the something in lora has been deleted.
            return address_dict

        for address in user_addresses:
            if address_dict.get(address["address_type"]["uuid"]) is not None:
                # More than one of this type exist in MO, this is not allowed.
                msg = "Inconsistent addresses for employee: {}"
                logger.error(msg.format(mo_uuid))
            address_dict[address["address_type"]["uuid"]] = {
                "value": address["value"],
                "uuid": address["uuid"],
            }
        return address_dict

    async def _condense_employee_opus_addresses(self, employee):
        opus_addresses = {}
        if "email" in employee and not self.settings.get(
            "integrations.opus.skip_employee_email", False
        ):
            opus_addresses["email"] = employee["email"]

        skip_employee_phone = self.settings.get(
            "integrations.opus.skip_employee_phone", False
        )
        if employee["workPhone"] is not None:
            phone = opus_helpers.parse_phone(employee["workPhone"])
            if (phone is not None) and (not skip_employee_phone):
                opus_addresses["phone"] = phone

        if (
            "postalCode" in employee
            and employee["address"]
            and not self.settings.get("integrations.opus.skip_employee_address", False)
        ):
            if isinstance(employee["address"], dict):
                logger.info("Protected addres, cannont import")
            else:
                address_string = employee["address"]
                zip_code = employee["postalCode"]
                address_uuid = await self.find_address(address_string, zip_code)
                if address_uuid:
                    opus_addresses["dar"] = address_uuid
                else:
                    logger.warning("Could not find address in DAR")
        return opus_addresses

    def _perform_address_update(self, args, current):
        addr_type = args["address_type"]["uuid"]
        if current is None:  # Create address
            payload = payloads.create_address(**args)
            logger.debug("Create {} address payload: {}".format(addr_type, payload))
            response = self.helper._mo_post("details/create", payload)
            assert response.status_code == 201
        elif (args.get("value") == current.get("value")) and (
            args.get("visibility") == current.get("visibility")
        ):  # Nothing changed
            logger.info("{} not updated".format(addr_type))
        else:  # Edit address
            payload = payloads.edit_address(args, current["uuid"])
            logger.debug("Edit address {}, payload: {}".format(addr_type, payload))
            response = self.helper._mo_post("details/edit", payload)
            response.raise_for_status()

    async def _update_employee_address(self, mo_uuid, employee):
        opus_addresses = await self._condense_employee_opus_addresses(employee)
        mo_addresses = self._condense_employee_mo_addresses(mo_uuid)
        logger.info("Addresses to be synced to MO: {}".format(opus_addresses))

        for addr_type, mo_addr_type in EMPLOYEE_ADDRESS_CHECKS.items():
            if opus_addresses.get(addr_type) is None:
                continue

            addr_type_uuid = self.ensure_class_in_facet(
                "employee_address_type",
                mo_addr_type,
                scope=predefined_scopes.get(mo_addr_type),
            )
            visibility = None
            if mo_addr_type == "Adresse":
                visibility = self.ensure_class_in_facet(
                    facet="visibility",
                    bvn="Intern",
                    title="Må vises internt",
                    scope="INTERNAL",
                )

            current = mo_addresses.get(str(addr_type_uuid))
            address_args = {
                "address_type": {"uuid": str(addr_type_uuid)},
                "value": opus_addresses[addr_type],
                "validity": {"from": self.xml_date.strftime("%Y-%m-%d"), "to": None},
                "user_uuid": mo_uuid,
            }
            if visibility:
                address_args["visibility"] = {"uuid": str(visibility)}
            self._perform_address_update(address_args, current)

    async def _update_unit_addresses(self, unit):
        calculated_uuid = opus_helpers.generate_uuid(unit["@id"])
        unit_addresses = self.helper.read_ou_address(
            calculated_uuid, scope=None, return_all=True
        )

        address_dict = {}
        for address in unit_addresses:
            if address_dict.get(address["type"]) is not None:
                # More than one of this type exist in MO, this is not allowed.
                msg = "Inconsistent addresses for unit: {}"
                logger.error(msg.format(calculated_uuid))
            # if address['value'] not in ('9999999999999', '0000000000'):
            address_dict[address["type"]] = {
                "value": address["value"],
                "uuid": address["uuid"],
            }

        if unit.get("street") and unit.get("zipCode"):
            address_uuid = await self.find_address(unit["street"], unit["zipCode"])
            if address_uuid:
                logger.debug("Found DAR uuid: {}".format(address_uuid))
                unit["dar"] = address_uuid
            else:
                logger.warning(
                    "Failed to lookup {}, {}".format(unit["street"], unit["zipCode"])
                )

        for addr_type, mo_addr_type in UNIT_ADDRESS_CHECKS.items():
            # addr_type is the opus name for the address, mo_addr_type
            # is read from MO
            if unit.get(addr_type) is None:
                continue

            addr_type_uuid = self.ensure_class_in_facet(
                "org_unit_address_type",
                mo_addr_type,
                scope=predefined_scopes.get(mo_addr_type),
            )
            current = address_dict.get(str(addr_type_uuid))
            args = {
                "address_type": {"uuid": str(addr_type_uuid)},
                "value": unit[addr_type],
                "validity": {"from": self.xml_date.strftime("%Y-%m-%d"), "to": None},
                "unit_uuid": str(calculated_uuid),
            }
            self._perform_address_update(args, current)

    async def update_unit(self, unit):
        calculated_uuid = opus_helpers.generate_uuid(unit["@id"])
        parent_name = unit["parentOrgUnit"]
        parent_uuid = (
            opus_helpers.generate_uuid(parent_name)
            if parent_name
            else self.helper.read_organisation()
        )
        mo_unit = self.helper.read_ou(calculated_uuid)

        # Default to "Enhed" as unittype
        org_type_title = unit.get("orgTypeTxt", "Enhed")
        org_type_bvn = unit.get("orgType", org_type_title)

        unit_type = self.ensure_class_in_facet(
            "org_unit_type", bvn=org_type_bvn, title=org_type_title
        )
        from_date = unit.get("startDate", "01-01-1900")
        unit_user_key = self.settings.get("integrations.opus.unit_user_key", "@id")
        unit_args = {
            "unit": unit,
            "unit_user_key": unit[unit_user_key],
            "unit_uuid": str(calculated_uuid),
            "unit_type": str(unit_type),
            "parent": str(parent_uuid),
            "from_date": from_date,
        }

        if mo_unit.get("uuid"):  # Edit
            unit_args["from_date"] = self.xml_date.strftime("%Y-%m-%d")
            payload = payloads.edit_org_unit(**unit_args)
            logger.info("Edit unit: {}".format(payload))
            response = self.helper._mo_post("details/edit", payload)
            if response.status_code == 400:
                assert response.text.find("raise to a new registration") > 0
            else:
                response.raise_for_status()
        else:  # Create
            payload = payloads.create_org_unit(**unit_args)
            logger.debug("Create department payload: {}".format(payload))
            response = self.helper._mo_post("ou/create", payload)
            response.raise_for_status()
            logger.info("Created unit {}".format(unit["@id"]))
            logger.debug("Response: {}".format(response.text))

        await self._update_unit_addresses(unit)

    def _job_and_engagement_type(self, employee):
        job = employee["position"]
        job_function_uuid = self.ensure_class_in_facet(
            "engagement_job_function", bvn=job
        )

        contract = employee.get("workContractText", "Ansat")
        engagement_type_uuid = self.ensure_class_in_facet("engagement_type", contract)
        return str(job_function_uuid), str(engagement_type_uuid)

    def update_engagement(self, mo_engagement, opus_employee):
        """
        Update a MO engagement according to opus employee object.
        It often happens that the change that provoked lastChanged to
        be updated is not a MO field, and thus we check for relevant
        differences before shipping the payload to MO.
        :param engagement: Relevant MO engagement object.
        :param employee: Relevent Opus employee object.
        :return: True if update happended, False if not.
        """
        job_function, eng_type = self._job_and_engagement_type(opus_employee)
        unit_uuid = opus_helpers.generate_uuid(opus_employee["orgUnit"])

        mo_start_date = datetime.fromisoformat(mo_engagement["validity"]["from"])
        opus_start_date = datetime.fromisoformat(opus_employee["entryDate"])
        # If the start date is the same in opus and MO we use an edit operation with a start date of current date.
        # If not we use the start date from opus. This allows setting the start date earlier than it was before in MO.
        edit = mo_start_date == opus_start_date
        validity = self.validity(opus_employee, edit=edit)
        data = {
            "engagement_type": {"uuid": eng_type},
            "job_function": {"uuid": job_function},
            "org_unit": {"uuid": str(unit_uuid)},
            "validity": validity,
        }

        engagement_unit = self.helper.read_ou(unit_uuid)
        if "error" in engagement_unit:
            msg = "The wanted unit does not exit: {}"
            logger.error(msg.format(unit_uuid))
            raise UnknownOpusUnit

        if mo_engagement["validity"]["to"] is None:
            old_valid_to = datetime.strptime("9999-12-31", "%Y-%m-%d")
        else:
            old_valid_to = datetime.strptime(
                mo_engagement["validity"]["to"], "%Y-%m-%d"
            )
        if validity["to"] is None:
            new_valid_to = datetime.strptime("9999-12-31", "%Y-%m-%d")
        else:
            new_valid_to = datetime.strptime(validity["to"], "%Y-%m-%d")

        something_new = any(
            (
                mo_engagement["engagement_type"]["uuid"] != eng_type,
                mo_engagement["job_function"]["uuid"] != job_function,
                mo_engagement["org_unit"]["uuid"] != str(unit_uuid),
                mo_start_date != opus_start_date,
                old_valid_to != new_valid_to,
            )
        )

        logger.info("Something new? {}".format(something_new))
        if something_new:
            payload = payloads.edit_engagement(data, mo_engagement["uuid"])
            logger.debug("Update engagement payload: {}".format(payload))
            response = self.helper._mo_post("details/edit", payload)
            self._assert(response)

        # Handle shortening engagements:
        # If an engagement ends earlier than expected we must terminate the engagement
        # from the new end date.
        if new_valid_to < old_valid_to:
            self.terminate_detail(
                mo_engagement["uuid"], detail_type="engagement", end_date=new_valid_to
            )
        # If an engagement starts later than expected we must terminate the engagement,
        # but only in the period from the original start date to the new start date.
        # Also we subtract one day from the end date such that the engagement will have
        # the correct start day in mo.
        if opus_start_date > mo_start_date:
            self.terminate_detail(
                mo_engagement["uuid"],
                detail_type="engagement",
                end_date=opus_start_date - timedelta(days=1),
                terminate_from_date=mo_start_date,
            )
        return something_new

    def create_engagement(self, mo_user_uuid, opus_employee):
        job_function, eng_type = self._job_and_engagement_type(opus_employee)
        unit_uuid = opus_helpers.generate_uuid(opus_employee["orgUnit"])

        engagement_unit = self.helper.read_ou(unit_uuid)
        if "error" in engagement_unit:
            msg = "The wanted unit does not exit: {}"
            logger.error(msg.format(opus_employee["orgUnit"]))
            raise UnknownOpusUnit

        validity = self.validity(opus_employee, edit=False)
        payload = payloads.create_engagement(
            employee=opus_employee,
            user_uuid=mo_user_uuid,
            unit_uuid=unit_uuid,
            job_function=job_function,
            engagement_type=eng_type,
            validity=validity,
        )
        logger.debug("Create engagement payload: {}".format(payload))
        response = self.helper._mo_post("details/create", payload)
        assert response.status_code == 201

    def create_user(self, employee, uuid=None):
        payload = payloads.create_user(employee, self.org_uuid, uuid)

        logger.info("Create user payload: {}".format(payload))
        r = self.helper._mo_post("e/create", payload)
        r.raise_for_status()
        return_uuid = r.json()
        logger.info(
            "Created employee {} {} with uuid {}".format(
                employee["firstName"], employee["lastName"], return_uuid
            )
        )

        return return_uuid

    def connect_it_system(self, username, it_system, employee, person_uuid):
        it_system_uuid = self.it_systems[it_system]
        try:
            current = self.helper.get_e_itsystems(
                person_uuid, it_system_uuid=it_system_uuid
            )
        except TypeError as e:
            if self.dry_run:
                logger.debug(
                    f"dry-run. Would connect ituser with {username=} to {it_system=} "
                )
                return
            raise (e)

        try:
            current = only(current, default={})
        except ValueError:
            logger.warning(
                f"Skipped connecting {it_system} IT system . More than one IT system found for {person_uuid=}"
            )
            return

        if not (username or current):
            return
        # New it-system account
        if not current:
            payload = payloads.connect_it_system_to_user(
                username,
                it_system_uuid,
                person_uuid,
                self.xml_date.strftime("%Y-%m-%d"),
            )
            logger.debug(f"{it_system} account payload: {payload}")
            response = self.helper._mo_post("details/create", payload)
            assert response.status_code == 201
            logger.info(f"Added {it_system} info for {person_uuid}")
        # Deleted it-system account
        elif not username:
            self.terminate_detail(current["uuid"], detail_type="it")
            logger.info(f"No {it_system} info for {person_uuid} any longer")
        # Changed account name. Only supports one account pr it-system
        elif current.get("user_key") != username:
            payload = payloads.edit_it_system_username(
                current["uuid"],
                username,
                self.xml_date.strftime("%Y-%m-%d"),
            )
            logger.debug(f"{it_system} account payload: {payload}")
            response = self.helper._mo_post("details/edit", payload)
            response.raise_for_status()
            logger.info(f"Changed {it_system} info for {person_uuid}")

    def _to_datetime(self, item):
        if item is None:
            item_datetime = datetime.strptime("9999-12-31", "%Y-%m-%d")
        else:
            item_datetime = datetime.strptime(item, "%Y-%m-%d")
        return item_datetime

    def update_manager_status(self, employee_mo_uuid, employee):
        url = "e/{}/details/manager?at=" + self.validity(employee, edit=True)["from"]
        all_manager_functions = self.helper._mo_lookup(employee_mo_uuid, url)
        manager_functions = [
            manager_function
            for manager_function in all_manager_functions
            if manager_function["user_key"] == employee["@id"]
        ]

        logger.debug("Manager functions to update: {}".format(manager_functions))
        if manager_functions:
            logger.debug("Manager functions to update: {}".format(manager_functions))

        if employee["isManager"] == "false":
            if manager_functions:
                logger.info("Terminate manager function")
                self.terminate_detail(
                    manager_functions[0]["uuid"], detail_type="manager"
                )
            else:
                logger.debug("Correctly not a manager")

        if employee["isManager"] == "true":
            manager_level = "{}.{}".format(
                employee["superiorLevel"], employee["subordinateLevel"]
            )
            manager_level_uuid = self.ensure_class_in_facet(
                "manager_level", manager_level
            )
            manager_type = employee["position"]
            manager_type_uuid = self.ensure_class_in_facet("manager_type", manager_type)
            responsibility_uuid = self.ensure_class_in_facet(
                "responsibility", "Lederansvar"
            )

            args = {
                "unit": str(opus_helpers.generate_uuid(employee["orgUnit"])),
                "person": employee_mo_uuid,
                "manager_type": str(manager_type_uuid),
                "level": str(manager_level_uuid),
                "responsibility": str(responsibility_uuid),
                "validity": self.validity(employee, edit=True),
            }
            if manager_functions:
                logger.info("Attempt manager update of {}:".format(employee_mo_uuid))
                # Opus supports only a single manager object pr engagement
                assert len(manager_functions) == 1

                mf = manager_functions[0]

                payload = payloads.edit_manager(
                    object_uuid=manager_functions[0]["uuid"], **args
                )

                something_new = not (
                    mf["org_unit"]["uuid"] == args["unit"]
                    and mf["person"]["uuid"] == args["person"]
                    and mf["manager_type"]["uuid"] == args["manager_type"]
                    and mf["manager_level"]["uuid"] == args["level"]
                    and mf["responsibility"][0]["uuid"] == args["responsibility"]
                )

                if something_new:
                    logger.debug("Something is changed, execute payload")
                else:
                    mo_end_datetime = self._to_datetime(mf["validity"]["to"])
                    opus_end_datetime = self._to_datetime(args["validity"]["to"])
                    logger.info("MO end datetime: {}".format(mo_end_datetime))
                    logger.info("OPUS end datetime: {}".format(opus_end_datetime))

                    if mo_end_datetime == opus_end_datetime:
                        logger.info("No edit of manager object")
                        payload = None
                    elif opus_end_datetime > mo_end_datetime:
                        logger.info("Extend validity, send payload to MO")
                    else:  # opus_end_datetime < mo_end_datetime:
                        logger.info("Terminate mangement role")
                        payload = None
                        self.terminate_detail(
                            mf["uuid"],
                            detail_type="manager",
                            end_date=opus_end_datetime,
                        )

                logger.debug("Update manager payload: {}".format(payload))
                if payload is not None:
                    response = self.helper._mo_post("details/edit", payload)
                    self._assert(response)
            else:  # No existing manager functions
                logger.info("Turn this person into a manager")
                args["validity"] = self.validity(employee, edit=False)
                payload = payloads.create_manager(user_key=employee["@id"], **args)
                logger.debug("Create manager payload: {}".format(payload))
                response = self.helper._mo_post("details/create", payload)
                assert response.status_code == 201

    async def update_employee(self, employee):
        cpr = opus_helpers.read_cpr(employee)
        logger.info("----")
        logger.info("Now updating {}".format(employee.get("@id")))
        logger.debug("Available info: {}".format(employee))
        if employee["position"] is None:
            logger.warning("Error in opus-file. Missing 'position'")
            return
        if employee["entryDate"] is None:
            logger.warning(
                "Error in opus-file. No entryDate for this employee. Skipping."
            )
            return
        mo_user = self.helper.read_user(user_cpr=cpr, use_cache=False)

        ad_info = {}
        if self.ad_reader is not None:
            ad_info = self.ad_reader.read_user(cpr=cpr)

        if mo_user is None:
            uuid = ad_info.get("ObjectGuid")
            employee_mo_uuid = self.create_user(employee, uuid)
        else:
            employee_mo_uuid = mo_user["uuid"]

            # Update user if name has changed
            if (employee["firstName"] != mo_user["givenname"]) or (
                employee["lastName"] != mo_user["surname"]
            ):
                employee_mo_uuid = self.create_user(employee, employee_mo_uuid)
                logger.info(f"Updated name of employee {employee_mo_uuid}")

        # Add it-systems
        self.connect_it_system(
            employee.get("userId"), constants.Opus_it_system, employee, employee_mo_uuid
        )

        if self.ad_reader is not None:
            sam_account = ad_info.get("SamAccountName")
            self.connect_it_system(
                sam_account, constants.AD_it_system, employee, employee_mo_uuid
            )

        await self._update_employee_address(employee_mo_uuid, employee)

        # Now we have a MO uuid, update engagement:
        mo_engagements = self.helper.read_user_engagement(
            employee_mo_uuid, read_all=True
        )
        user_engagements = filter(
            lambda eng: eng["user_key"] == employee["@id"], mo_engagements
        )
        current_mo_eng = None
        for eng in user_engagements:
            current_mo_eng = eng["uuid"]
            val_from = datetime.strptime(eng["validity"]["from"], "%Y-%m-%d")
            val_to = datetime.strptime("9999-12-31", "%Y-%m-%d")
            if eng["validity"]["to"] is not None:
                val_to = datetime.strptime(eng["validity"]["to"], "%Y-%m-%d")
            if val_from < self.xml_date < val_to:
                logger.info("Found current validty {}".format(eng["validity"]))
                break

        if current_mo_eng is None:
            self.create_engagement(employee_mo_uuid, employee)
        else:
            logger.info("Validity for {}: {}".format(employee["@id"], eng["validity"]))
            self.update_engagement(eng, employee)

        try:
            self.update_manager_status(employee_mo_uuid, employee)
        except TypeError as e:
            if self.dry_run:
                logger.debug("dry-run. Would update manager status for the new user")
                return
            raise (e)

    def terminate_detail(
        self,
        uuid,
        detail_type="engagement",
        end_date: datetime | None = None,
        terminate_from_date: datetime | None = None,
    ):
        if end_date is None:
            end_date = self.xml_date

        payload = payloads.terminate_detail(
            uuid, end_date, detail_type, terminate_from=terminate_from_date
        )
        logger.debug("Terminate payload: {}".format(payload))
        response = self.helper._mo_post("details/terminate", payload)
        logger.debug("Terminate response: {}".format(response.text))
        self._assert(response)

    def find_unterminated_filtered_units(self, units):
        """Check if units are in MO."""

        # Read all active MO org_units
        mo_units = self.helper.read_ou_root(
            org=self.helper.read_organisation(),
        )
        current_uuids = set(map(itemgetter("uuid"), mo_units))
        # return the units that are active in os2mo, but should be terminated
        mo_units = filter(
            lambda unit: opus_helpers.gen_unit_uuid(unit) in current_uuids, units
        )
        return mo_units

    def handle_filtered_units(self, units, dry_run=False):
        """Rules for handling units that are not imported from opus.

        If a unit is filtered from the Opus file it means it cannot be deleted in Opus, but should not appear in MO.
        Any units that exists in MO, but are later moved in Opus to be below one of the filtered units should be terminated in MO.
        """
        unfiltered_units = {opus_helpers.gen_unit_uuid(unit) for unit in units}
        if dry_run:
            print(
                f"There are {len(unfiltered_units)} units that should have been terminated."
            )
            if units:
                print(unfiltered_units)
            return

        for uuid in tqdm(unfiltered_units, desc="Terminating filtered org_units"):
            # TODO: Use actual org_unit terminate endpoint for this.
            self.terminate_detail(uuid, detail_type="org_unit", end_date=self.xml_date)

    def delete_engagement(self, opus_id: int) -> None:
        """Find and delete an engagement in MO based on its opus-identifier"""
        eng_uuid = self._find_engagement(
            opus_id,
        )
        if not eng_uuid:
            logger.debug("Cancelled engagement is already deleted")
            return

        res = self.gql_client.execute(
            MUTATION_DELETE_ENGAGEMENT, variable_values={"uuid": eng_uuid}
        )
        assert (
            res["engagement_delete"]["uuid"] == eng_uuid
        )  # Just a check that it actually worked as intended
        logger.debug("Cancelled engagement deleted")

    async def start_import(
        self, units, employees, terminated_employees, cancelled_employees
    ):
        """
        Start an opus import, run the oldest available dump that
        has not already been imported.
        """

        for unit in tqdm(units, desc="Update units"):
            await self.update_unit(unit)

        for employee in tqdm(employees, desc="Update employees"):
            await self.update_employee(employee)

        for employee in tqdm(terminated_employees, desc="Terminating employees"):
            # This is a terminated employee, check if engagement is active
            # terminate if it is.
            if not employee["@action"] == "leave":
                msg = "This should be a terminated employee!"
                logger.error(msg)
                raise Exception(msg)

            eng_info = self._find_engagement(employee["@id"], present=True)
            if eng_info:
                logger.info("Terminating: {}".format(eng_info))
                self.terminate_detail(eng_info)
                manager_info = self._find_manager_role(employee["@id"], present=True)
                if manager_info:
                    self.terminate_detail(manager_info, detail_type="manager")

        for cancelled in tqdm(cancelled_employees):
            self.delete_engagement(int(cancelled["@id"]))
        logger.info("Program ended correctly")


async def import_one(
    ad_reader,
    xml_date: datetime,
    latest_date: Optional[datetime],
    dumps: Dict,
    filter_ids: List[str],
    opus_id: Optional[int] = None,
    rundb_write=True,
    dry_run=False,
):
    """Import one file at the date xml_date."""
    msg = "Start update: File: {}, update since: {}"
    logger.info(msg.format(xml_date, latest_date))
    print(msg.format(xml_date, latest_date))
    # Find changes to units and employees
    latest_path = None
    if latest_date:
        latest_path = dumps[latest_date]
    xml_path = dumps[xml_date]
    (
        units,
        filtered_units,
        employees,
        terminated_employees,
        cancelled_employees,
    ) = opus_helpers.read_and_transform_data(
        latest_path, xml_path, filter_ids, opus_id=opus_id
    )
    if rundb_write and not dry_run:
        opus_helpers.local_db_insert((xml_date, "Running diff update since {}"))

    diff = OpusDiffImport(
        xml_date,
        ad_reader=ad_reader,
        filter_ids=filter_ids,
        dry_run=dry_run,
    )
    await diff.start_import(units, employees, terminated_employees, cancelled_employees)
    filtered_units = diff.find_unterminated_filtered_units(filtered_units)

    diff.handle_filtered_units(filtered_units)
    if rundb_write and not dry_run:
        opus_helpers.local_db_insert((xml_date, "Diff update ended: {}"))
    print()


async def start_opus_diff(ad_reader=None, dry_run: bool = False):
    """
    Start an opus update, use the oldest available dump that has not
    already been imported.
    """
    SETTINGS = load_settings()

    dumps = opus_helpers.read_available_dumps()
    run_db = Path(SETTINGS["integrations.opus.import.run_db"])
    filter_ids = SETTINGS.get("integrations.opus.units.filter_ids", [])

    if not run_db.is_file():
        logger.error("Local base not correctly initialized")
        raise RunDBInitException("Local base not correctly initialized")
    xml_date, latest_date = opus_helpers.next_xml_file(run_db, dumps)

    while xml_date:
        await import_one(
            ad_reader,
            xml_date,  # type: ignore
            latest_date,  # type: ignore
            dumps,
            filter_ids,
            opus_id=None,
            dry_run=dry_run,
        )
        # Check if there are more files to import
        xml_date, latest_date = opus_helpers.next_xml_file(run_db, dumps)
        logger.info("Ended update")


if __name__ == "__main__":
    settings = load_settings()
    reader: ad_reader.ADParameterReader | LdapADGUIDReader | None = None
    if (hostname := settings.get("integrations.opus.ldap_url")) and (
        port := settings.get("integrations.opus.ldap_port")
    ):
        reader = LdapADGUIDReader(host=hostname, port=port)
    elif settings.get("integrations.ad"):
        reader = ad_reader.ADParameterReader()

    try:
        asyncio.run(start_opus_diff(ad_reader=reader))
    except RunDBInitException:
        print("RunDB not initialized")
