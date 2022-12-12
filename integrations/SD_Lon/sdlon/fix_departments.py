import datetime
import json
import logging
from functools import partial
from itertools import chain
from typing import Any
from typing import Optional
from typing import List
from typing import OrderedDict

import click
import requests
from os2mo_helpers.mora_helpers import MoraHelper

from sdlon.models import SDGetDepartmentReq
from sdlon.sd_client import SDClient
from .date_utils import (
    datetime_to_sd_date,
    parse_date,
    SD_INFINITY,
    MO_INFINITY,
    format_date,
)
from . import sd_payloads
from .config import ChangedAtSettings
from .config import get_changed_at_settings
from .exceptions import NoCurrentValdityException
from .sd_common import mora_assert
from .sd_common import sd_lookup as _sd_lookup


sd_lookup = partial(_sd_lookup, use_cache=False)

LOG_LEVEL = logging.DEBUG
LOG_FILE = "fix_sd_departments.log"

logger = logging.getLogger("fixDepartments")


def setup_logging():
    detail_logging = ("sdCommon", "fixDepartments")
    for name in logging.root.manager.loggerDict:
        if name in detail_logging:
            logging.getLogger(name).setLevel(LOG_LEVEL)
        else:
            logging.getLogger(name).setLevel(logging.ERROR)

    logging.basicConfig(
        format="%(levelname)s %(asctime)s %(name)s %(message)s",
        level=LOG_LEVEL,
        filename=LOG_FILE,
    )


class FixDepartments:
    def __init__(self, settings: ChangedAtSettings):
        logger.info("Start program")
        self.settings = settings

        self.institution_uuid = self.get_institution()
        self.helper = self._get_mora_helper(self.settings)

        if self.settings.sd_fix_departments_root is not None:
            self.org_uuid = str(settings.sd_fix_departments_root)
        else:
            try:
                self.org_uuid = self.helper.read_organisation()
            except requests.exceptions.RequestException as e:
                logger.error(e)
                print(e)
                exit()
            except json.decoder.JSONDecodeError as e:
                logger.error(e)
                print(e)
                exit()

        logger.info("Read org_unit types")
        self.level_types = self.helper.read_classes_in_facet("org_unit_level")[0]
        unit_types = self.helper.read_classes_in_facet("org_unit_type")[0]

        # Currently only a single unit type exists, we will not do anything fancy
        # until it has been decided what the source for types should be.
        self.unit_type = None
        for unit in unit_types:
            if unit["user_key"] == "Enhed":
                self.unit_type = unit

        if self.unit_type is None:
            raise Exception("Unit types not correctly configured")

    def _get_mora_helper(self, settings):
        return MoraHelper(hostname=self.settings.mora_base, use_cache=False)

    def get_institution(self):
        """
        Get the institution uuid of the current organisation. It is uniquely
        determined from the InstitutionIdentifier. The identifier is read
        from settings.json. The value is rarely used, but is needed to dertermine
        if a unit is a root unit.
        :return: The SD institution uuid for the organisation.
        """
        inst_id = self.settings.sd_institution_identifier
        params = {"UUIDIndicator": "true", "InstitutionIdentifier": inst_id}
        institution_info = sd_lookup(
            "GetInstitution20111201", settings=self.settings, params=params
        )
        institution = institution_info["Region"]["Institution"]
        institution_uuid = institution["InstitutionUUIDIdentifier"]
        return institution_uuid

    def create_single_department(
        self, department: OrderedDict, parent_uuid: str
    ) -> None:
        """
        Create an organization unit in MO based on the corresponding department info
        from SD.

        Args:
            department: The SD department
            parent_uuid: The parent UUID of the department in SD
        """
        logger.info(
            f"""Create department with SD UUID: {department["DepartmentUUIDIdentifier"]}"""
        )

        effective_parent_uuid = (
            parent_uuid if parent_uuid is not None else self.org_uuid
        )

        for unit_level in self.level_types:
            if unit_level["user_key"] == department["DepartmentLevelIdentifier"]:
                unit_level_uuid = unit_level["uuid"]

        logger.debug("SD department info to create from: {}".format(department))
        payload = sd_payloads.create_single_org_unit(
            department=department,
            unit_type=self.unit_type["uuid"],
            unit_level=unit_level_uuid,
            parent=effective_parent_uuid,
        )
        logger.debug("Create department MO payload: {}".format(payload))
        response = self.helper._mo_post("ou/create", payload)
        response.raise_for_status()
        logger.info("Created unit {}".format(department["DepartmentIdentifier"]))
        logger.debug("Create response status: {}".format(response.status_code))

    def _create_org_unit_if_missing_in_mo(
        self, department: OrderedDict, parent_uuid: Optional[str]
    ) -> bool:
        """
        Create SD department in MO if it is missing.

        Args:
            department: the SD department
            parent_uuid: UUID of the parent unit

        Returns:
            Boolean indicating whether the unit was created in MO or not.
        """

        mo_response = self.helper.read_ou(
            department["DepartmentUUIDIdentifier"], at=department["ActivationDate"]
        )

        ou_created = False
        if mo_response.get("status") == 404:
            # TODO: create_single_department should return the boolean value of ou_created
            self.create_single_department(department, parent_uuid)
            ou_created = True

        return ou_created

    def _update_org_unit_for_single_sd_dep_registration(
        self, department: OrderedDict, parent_uuid: Optional[str]
    ) -> None:
        # Get SD department data
        unit_uuid = department["DepartmentUUIDIdentifier"]
        name = department["DepartmentName"]
        shortname = department["DepartmentIdentifier"]
        department_level_identifier = department["DepartmentLevelIdentifier"]
        from_date = department["ActivationDate"]
        to_date = department["DeactivationDate"]
        to_date = MO_INFINITY if to_date == SD_INFINITY else to_date

        unit_level_uuid = None
        for unit_level in self.level_types:
            if unit_level["user_key"] == department_level_identifier:
                unit_level_uuid = unit_level["uuid"]
        if unit_level_uuid is None:
            msg = "Unknown department level {}!!"
            logger.error(msg.format(department_level_identifier))
            raise Exception(msg.format(department_level_identifier))

        parent = parent_uuid if parent_uuid is not None else self.org_uuid

        msg = "Unit parent at {} is {}"
        logger.info(msg.format(from_date, parent))

        payload = sd_payloads.edit_org_unit(
            user_key=shortname,
            name=name,
            unit_uuid=unit_uuid,
            parent=parent,
            ou_level=unit_level_uuid,
            ou_type=self.unit_type["uuid"],
            from_date=from_date,
            to_date=to_date,
        )
        logger.debug("Edit payload to fix unit: {}".format(payload))
        response = self.helper._mo_post("details/edit", payload)
        logger.debug("Edit response status: {}".format(response.status_code))
        if response.status_code == 400:
            assert response.text.find("raise to a new registration") > 0
        else:
            response.raise_for_status()

    def fix_department(
        self,
        unit_uuid: str,
        validity_date: datetime.datetime,
        fix_departments_recursively: bool = True
    ) -> None:
        """
        Synchronize the state of a MO unit to the current and future state(s) in SD.
        :param unit_uuid: uuid of the unit to be updated.
        :param validity_date: The validity date to read the department info from SD.
        :param fix_departments_recursively: if true, fix departments recursively from the given unit and up the tree
        """
        msg = "Set department {} to state as of {}"
        logger.info(msg.format(unit_uuid, validity_date))
        validity = {
            "from_date": validity_date.strftime("%d.%m.%Y"),
            "to_date": datetime_to_sd_date(parse_date(SD_INFINITY)),
        }

        departments = self.get_department(validity, uuid=unit_uuid)
        for department in departments:
            # Get the UUID of the parent unit
            parent_uuid = self.get_parent(
                unit_uuid, parse_date(department["ActivationDate"])
            )

            # Create org unit if missing in MO
            ou_created = self._create_org_unit_if_missing_in_mo(department, parent_uuid)

            # ... and fix the parent before updating the unit itself
            if fix_departments_recursively and parent_uuid is not None:
                self.fix_department(parent_uuid, validity_date)

            if not ou_created:
                self._update_org_unit_for_single_sd_dep_registration(
                    department, parent_uuid
                )

    def get_department(
        self, validity, shortname=None, uuid=None
    ) -> List[OrderedDict[str, Any]]:
        """
        Read department information from SD.
        NOTICE: Shortnames are not universally unique in SD, and even a request
        spanning a single date might return more than one row if searched by
        shortname.
        :param validity: Validity dictionaty.
        :param shortname: Shortname for the unit(s).
        :param uuid: uuid for the unit.
        :return: A list of information about the unit(s).
        """
        params = {
            "ActivationDate": validity["from_date"],
            "DeactivationDate": validity["to_date"],
            "ContactInformationIndicator": "true",
            "DepartmentNameIndicator": "true",
            "PostalAddressIndicator": "false",
            "ProductionUnitIndicator": "false",
            "UUIDIndicator": "true",
            "EmploymentDepartmentIndicator": "false",
        }
        if uuid is not None:
            params["DepartmentUUIDIdentifier"] = uuid
        if shortname is not None:
            params["DepartmentIdentifier"] = shortname

        if uuid is None and shortname is None:
            raise Exception("Provide either uuid or shortname")

        department_info = sd_lookup(
            "GetDepartment20111201", settings=self.settings, params=params
        )
        department = department_info.get("Department")
        if department is None:
            raise NoCurrentValdityException()
        if isinstance(department, OrderedDict):
            department = [department]
        return department

    # Notice! Similar code also exists in sd_changed_at
    def _find_engagement(self, mo_engagements, job_id):
        """
        Given a list of engagements for a person, find the one with a specific
        job_id. If several elements covering the same engagement is in the list
        an unspecified element will be returned.
        :param mo_engaements: A list of engagements as returned by MO.
        :param job_id: The SD JobIdentifier to find.
        :return: Some element in the list that has the correct job_id. If no
        engagement is found, None is returned.
        """
        relevant_engagement = None
        try:
            user_key = str(int(job_id)).zfill(5)
        except ValueError:  # We will end here, if int(job_id) fails
            user_key = job_id

        for mo_eng in mo_engagements:
            if mo_eng["user_key"] == user_key:
                relevant_engagement = mo_eng

        if relevant_engagement is None:
            msg = "Fruitlessly searched for {} in {}".format(job_id, mo_engagements)
            logger.info(msg)
        return relevant_engagement

    def _read_department_engagements(self, unit_uuid, validity_date):
        """
        Retrive a list from SD with all engagements in a given department.
        All current (as of validity_date) and future engagements are retrived,
        since GetEngagement does not support time ranges, we ask for three
        points in time that should cover all known future.
        :param unit_uuid: uuid of the relevant department.
        :param validity_date: The origin of the query, all engagements newer than
        this will be retrieved.
        :return: Dict with cpr as key and SD Person objects as values.
        """
        fix_date = validity_date + datetime.timedelta(weeks=80)
        too_deep = self.settings.sd_import_too_deep
        sd_validity = {
            "from_date": fix_date.strftime("%d.%m.%Y"),
            "to_date": fix_date.strftime("%d.%m.%Y"),
        }
        department = self.get_department(sd_validity, uuid=unit_uuid)[0]
        if not department["DepartmentLevelIdentifier"] in too_deep:
            msg = "{} regnes ikke som et SD afdelingsniveau"
            logger.info(msg.format(unit_uuid))
            return {}

        params = {
            "DepartmentIdentifier": department["DepartmentIdentifier"],
            "DepartmentLevelIdentifier": department["DepartmentLevelIdentifier"],
            "StatusActiveIndicator": True,
            "StatusPassiveIndicator": False,
            "DepartmentIndicator": True,
            "UUIDIndicator": True,
        }

        # We need to catch all current and future engagements, this is an attempt to
        # do so, without making too many calls to the api.
        time_deltas = [0, 90, 365]

        all_people = {}
        logger.debug("Perform GetEmployments, time_delas: {}".format(time_deltas))
        for time_delta in time_deltas:
            effective_date = validity_date + datetime.timedelta(days=time_delta)
            params["EffectiveDate"] = (effective_date.strftime("%d.%m.%Y"),)

            employments = sd_lookup(
                "GetEmployment20111201", settings=self.settings, params=params
            )
            people = employments.get("Person", [])
            if not isinstance(people, list):
                people = [people]

            for person in people:
                cpr = person["PersonCivilRegistrationIdentifier"]
                if cpr not in all_people:
                    all_people[cpr] = person
        logger.debug("Department engagements: {}".format(all_people.keys()))
        return all_people

    def fix_NY_logic(self, unit_uuid, validity_date):
        """
        Read all engagements in a unit and ensure that the position in MO is correct
        according to the rules of the import (ie, move engagement up from
        'Afdeling'). This is mainly relevant if an 'Afdeling' is moved to have a new
        NY-department as parent.
        Notice that this should be called AFTER the recursive fix of the department
        tree to ensure that the logic is applied to a self-consistent tree.

        If an engagement is already correct, it will not be moved, if it is currently
        in a wrong unit, it will be corrected.
        :param unit_uuid: uuid of the unit to check.
        :validity_date: The validity_date of the operation, moved engagements will
        be moved as of this date.
        """
        too_deep = self.settings.sd_import_too_deep
        mo_unit = self.helper.read_ou(unit_uuid)
        while mo_unit["org_unit_level"]["user_key"] in too_deep:
            mo_unit = mo_unit["parent"]
            logger.debug("Parent unit: {}".format(mo_unit["uuid"]))
        destination_unit = mo_unit["uuid"]
        logger.debug("Destination found: {}".format(destination_unit))

        all_people = self._read_department_engagements(unit_uuid, validity_date)

        # We now have a list of all current and future people in the unit,
        # they should all be unconditionally moved if they are not already
        # in destination_unit.
        for person in all_people.values():
            cpr = person["PersonCivilRegistrationIdentifier"]

            if not isinstance(person["Employment"], list):
                person["Employment"] = [person["Employment"]]

            for employment in person["Employment"]:
                job_id = employment["EmploymentIdentifier"]
                msg = "Checking job-id: {}"
                print(msg.format(job_id))
                logger.info(msg.format(job_id))
                sd_uuid = employment["EmploymentDepartment"]["DepartmentUUIDIdentifier"]
                if not sd_uuid == unit_uuid:
                    # This employment is not from the current department,
                    # but is inherited from a lower level. Can happen if this
                    # tool is initiated on a level higher than Afdelings-niveau.
                    continue

                mo_person = self.helper.read_user(user_cpr=cpr, org_uuid=self.org_uuid)
                if mo_person is None:
                    msg = "MO person is None for job_id: {}"
                    logger.warning(msg.format(job_id))
                    continue

                mo_engagements = self.helper.read_user_engagement(
                    mo_person["uuid"], read_all=True, only_primary=True, skip_past=True
                )

                # Find the uuid of the relevant engagement and update all current and
                # future rows.
                mo_engagement = self._find_engagement(mo_engagements, job_id)
                if mo_engagement is None:
                    msg = "MO engagement is None for job_id: {}, user_uuid: {}"
                    logger.warning(msg.format(job_id, mo_person["uuid"]))
                    continue
                for eng in mo_engagements:
                    if not eng["uuid"] == mo_engagement["uuid"]:
                        # This engagement is not relevant for this unit
                        continue
                    if eng["org_unit"]["uuid"] == destination_unit:
                        # This engagement is already in the correct unit
                        continue

                    from_date = datetime.datetime.strptime(
                        eng["validity"]["from"], "%Y-%m-%d"
                    )
                    if from_date < validity_date:
                        eng["validity"]["from"] = validity_date.strftime("%Y-%m-%d")

                    data = {
                        "org_unit": {"uuid": destination_unit},
                        "validity": eng["validity"],
                    }
                    payload = sd_payloads.engagement(data, mo_engagement)
                    logger.debug("Move engagement payload: {}".format(payload))
                    response = self.helper._mo_post("details/edit", payload)
                    mora_assert(response)

    def get_parent(self, unit_uuid, validity_date) -> Optional[str]:
        """
        Return the parent of a given department at at given point in time.
        Notice that the query is perfomed against SD, not against MO.
        It is generally not possible to predict whether this call will succeed, since
        this depends on the internal start-date at SD, which cannot be read from the
        API; the user of this function should be prepared to handle
        NoCurrentValdityException, unless the validity of the unit is known from
        other sources. In general queries to the future and near past should always
        be safe if the unit exists at the point in time.
        :param unit_uuid: uuid of the unit to be queried.
        :param validity_date: python datetime object with the date to query.
        :return: uuid of the parent department, None if the department is a root.
        """
        params = {
            "EffectiveDate": validity_date.strftime("%d.%m.%Y"),
            "DepartmentUUIDIdentifier": unit_uuid,
        }
        parent_response = sd_lookup(
            "GetDepartmentParent20190701", settings=self.settings, params=params
        )
        if "DepartmentParent" not in parent_response:
            msg = "No parent for {} found at validity: {}"
            logger.error(msg.format(unit_uuid, validity_date))
            raise NoCurrentValdityException()
        parent = parent_response["DepartmentParent"]["DepartmentUUIDIdentifier"]
        if parent == self.institution_uuid:
            parent = None
        return parent

    def get_all_parents(self, leaf_uuid, validity_date):
        """
        Find all parents from leaf unit up to the root of the tree.
        Notice, this is a query to SD, not to MO.
        :param leaf_uuid: The starting point of the chain, this does not stictly need
        to be a leaf node.
        :validity_date: The validity date of the fix.
        :return: A list of tuples containing short names and unit uuids sorted from leaf to root.
        """
        validity = {
            "from_date": validity_date.strftime("%d.%m.%Y"),
            "to_date": validity_date.strftime("%d.%m.%Y"),
        }
        department_branch = []
        department = self.get_department(validity=validity, uuid=leaf_uuid)[0]
        department_branch.append((department["DepartmentIdentifier"], leaf_uuid))

        current_uuid = self.get_parent(
            department["DepartmentUUIDIdentifier"], validity_date=validity_date
        )

        while current_uuid is not None:
            current_uuid = self.get_parent(
                department["DepartmentUUIDIdentifier"], validity_date=validity_date
            )
            department = self.get_department(validity=validity, uuid=current_uuid)[0]
            shortname = department["DepartmentIdentifier"]
            level = department["DepartmentLevelIdentifier"]
            uuid = department["DepartmentUUIDIdentifier"]
            department_branch.append((shortname, uuid))
            current_uuid = self.get_parent(current_uuid, validity_date=validity_date)
            msg = "Department: {}, uuid: {}, level: {}"
            logger.debug(msg.format(shortname, uuid, level))
        return department_branch

    def sd_uuid_from_short_code(self, validity_date, shortname):
        validity = {
            "from_date": validity_date.strftime("%d.%m.%Y"),
            "to_date": validity_date.strftime("%d.%m.%Y"),
        }
        department = self.get_department(validity, shortname=shortname)[0]
        return department["DepartmentUUIDIdentifier"]


@click.group()
def cli():
    setup_logging()


@cli.command()
@click.option(
    "--department-short-name",
    "short_names",
    multiple=True,
    type=click.STRING,
    help="Shortname of the department to update",
)
@click.option(
    "--department-uuid",
    "uuids",
    multiple=True,
    type=click.UUID,
    help="UUID of the department to update",
)
def unit_fixer(short_names, uuids):
    """Sync SD department information to MO."""
    settings = get_changed_at_settings()
    unit_fixer = FixDepartments(settings)

    today = datetime.datetime.today()

    # Translate short_names to uuids
    short_name_uuids = map(
        partial(unit_fixer.sd_uuid_from_short_code, today), short_names
    )
    # Convert UUIDs to strings
    department_uuids = map(str, uuids)

    for department_uuid in chain(short_name_uuids, department_uuids):
        unit_fixer.fix_department(department_uuid, today)
        unit_fixer.fix_NY_logic(department_uuid, today)


@cli.command()
def fix_entire_ou_tree():
    settings = get_changed_at_settings()

    fd = FixDepartments(settings)
    sd = SDClient(settings.sd_user, settings.sd_password.get_secret_value())

    today = datetime.datetime.today()

    sd_department_resp = sd.get_department(
        SDGetDepartmentReq(
            InstitutionIdentifier=settings.sd_institution_identifier,
            ActivationDate=today.date(),
            DeactivationDate=today.date(),
            DepartmentNameIndicator=True,
            UUIDIndicator=True,
        )
    )
    for department in sd_department_resp.departments:
        logger.debug(
            f"Fixing {department.DepartmentName} with UUID {str(department.DepartmentUUIDIdentifier)}..."
        )
        fd.fix_department(
            str(department.DepartmentUUIDIdentifier), today, False
        )
        logger.info(
            f"Fixed {department.DepartmentName} with UUID {str(department.DepartmentUUIDIdentifier)}"
        )


if __name__ == "__main__":
    cli()
