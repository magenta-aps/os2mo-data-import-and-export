from datetime import datetime
from typing import Optional
from uuid import UUID

from integrations.opus import opus_helpers


def create_user(employee, org_uuid, uuid=None):
    if employee["firstName"] is None and employee["lastName"] is None:
        employee["firstName"] = "Ukendt"
        employee["lastName"] = "Ukendt"
        empl = dict(employee)
        empl.pop("cpr")
        print("No names on user")
        print(empl)
    cpr = opus_helpers.read_cpr(employee)
    payload = {
        "given_name": employee["firstName"],
        "surname": employee["lastName"],
        "cpr_number": cpr,
    }
    if uuid is not None:
        payload["uuid"] = uuid
    return payload


def edit_engagement(data, mo_engagement_uuid):
    payload = {"uuid": mo_engagement_uuid, **data}
    return payload


def create_engagement(
    employee, user_uuid, unit_uuid, job_function, engagement_type, validity
):
    payload = {
        "org_unit": str(unit_uuid),
        "person": user_uuid,
        "job_function": job_function,
        "engagement_type": engagement_type,
        "user_key": employee["@id"],
        "validity": validity,
    }
    return payload


def create_org_unit(unit, unit_user_key, unit_uuid, parent, unit_type, from_date):
    payload = {
        "uuid": unit_uuid,
        "user_key": unit_user_key,
        "name": unit["longName"],
        "parent": parent,
        "org_unit_type": unit_type,
        "validity": {"from": from_date, "to": None},
    }
    return payload


def edit_org_unit(unit, unit_user_key, unit_uuid, parent, unit_type, from_date):
    payload = {
        "uuid": unit_uuid,
        "user_key": unit_user_key,
        "name": unit["longName"],
        "parent": parent,
        "org_unit_type": unit_type,
        "validity": {"from": from_date, "to": None},
    }
    return payload


def terminate_detail(
    uuid: str,
    terminate_date: datetime,
    detail_type: str,
    terminate_from: datetime | None = None,
):
    """
    Create a payload for terminating details eg. engagements, manager-roles etc.

    Args:
        uuid: string representation of the uuid for the object to be terminated
        terminate_date: the last active date for the object
        detail_type: eg. engagement, address, manager. Not included in the
            returned payload; the caller uses it to select the GraphQL
            ``*_terminate`` mutation.
        terminate_from: optional first date of termination. If this is set the object will be terminated
        in the interval from terminate_from to terminate_date. This is used to move the startdate of engagements.
        In this case terminal_date will be the last inactive date for the object.
    """
    payload = {
        "uuid": uuid,
        "validity": {"to": terminate_date.strftime("%Y-%m-%d")},
    }
    if terminate_from:
        payload["validity"]["from"] = terminate_from.strftime("%Y-%m-%d")  # type: ignore
    return payload


def terminate_manager(uuid, terminate_date):
    payload = {"uuid": uuid, "validity": {"to": terminate_date}}
    return payload


def connect_it_system_to_user(username, it_system, person_uuid, from_date):
    payload = {
        "user_key": username,
        "itsystem": it_system,
        "person": person_uuid,
        "validity": {"from": from_date, "to": None},
    }
    return payload


def edit_it_system_username(uuid, username, from_date):
    payload = {
        "uuid": uuid,
        "user_key": username,
        "validity": {"from": from_date, "to": None},
    }
    return payload


def create_address(
    validity,
    address_type,
    value,
    unit_uuid=None,
    user_uuid=None,
    visibility: Optional[UUID] = None,
):
    if unit_uuid is None and user_uuid is None:
        raise Exception("Either unit or user must be specified")
    if unit_uuid and user_uuid:
        raise Exception("Only a unit or a person can be specified")

    payload = {
        "value": value,
        "address_type": address_type["uuid"]
        if isinstance(address_type, dict)
        else address_type,
        "validity": validity,
        "visibility": visibility["uuid"]
        if isinstance(visibility, dict)
        else visibility,
    }

    if unit_uuid is not None:
        payload["org_unit"] = unit_uuid
    if user_uuid is not None:
        payload["person"] = user_uuid
    return payload


def edit_address(data, mo_address_uuid):
    payload = {"uuid": mo_address_uuid, **data}
    return payload


def create_manager(
    user_key, unit, person, manager_type, level, responsibility, validity
):
    payload = {
        "user_key": user_key,
        "org_unit": unit,
        "person": person,
        "manager_type": manager_type,
        "manager_level": level,
        "responsibility": [responsibility],
        "validity": validity,
    }
    return payload


def edit_manager(
    object_uuid, unit, person, manager_type, level, responsibility, validity
):
    payload = {
        "uuid": object_uuid,
        "org_unit": unit,
        "person": person,
        "manager_type": manager_type,
        "manager_level": level,
        "responsibility": [responsibility],
        "validity": validity,
    }
    return payload
