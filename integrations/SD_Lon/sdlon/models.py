from datetime import datetime, date
from enum import Enum
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, root_validator, SecretStr


class JobFunction(str, Enum):
    job_position_identifier = "JobPositionIdentifier"
    employment_name = "EmploymentName"


class SDAuth(BaseModel):
    username: str
    password: SecretStr


class SDBasePerson(BaseModel):
    cpr: str
    given_name: Optional[str]
    surname: Optional[str]


class SDDepartment(BaseModel):
    ActivationDate: date
    DeactivationDate: date
    DepartmentIdentifier: str
    DepartmentLevelIdentifier: str
    DepartmentName: Optional[str]
    DepartmentUUIDIdentifier: Optional[UUID]


class SDGetDepartmentReq(BaseModel):
    """
    Query parameters for SDs GetDepartment20111201 endpoint
    """
    InstitutionIdentifier: Optional[str]
    InstitutionUUIDIdentifier: Optional[UUID]
    DepartmentIdentifier: Optional[str]
    DepartmentUUIDIdentifier: Optional[UUID]
    ActivationDate: date
    DeactivationDate: date
    # ContactInformationIndicator: bool = False
    DepartmentNameIndicator: bool = False
    # EmploymentDepartmentIndicator: bool = False
    # PostalAddressIndicator: bool = False
    # ProductionUnitIndicator: bool = False
    UUIDIndicator: bool = False

    # TODO: check what is actually required
    @root_validator
    def check_values(cls, values):
        institution_identifier = values.get("InstitutionIdentifier")
        institution_uuid_identifier = values.get("InstitutionUUIDIdentifier")
        department_identifier = values.get("DepartmentIdentifier")
        department_uuid_identifier = values.get("DepartmentUUIDIdentifier")

        if institution_identifier is None and institution_uuid_identifier is None:
            raise ValueError("Exactly one of InstitutionIdentifier or InstitutionUUIDndentifier can be set")
        if institution_identifier is not None and institution_uuid_identifier is not None:
            raise ValueError("Only one of InstitutionIdentifier and InstitutionUUIDIdentifier can be set")
        if department_identifier is not None and department_uuid_identifier is not None:
            raise ValueError("Only one of DepartmentIdentifier and DepartmentUUIDIdentifier can be set")

        return values


class SDGetDepartmentResp(BaseModel):
    """
    Response model for SDs GetDepartment20111201
    """
    # TODO: add missing fields
    region_identifier: str
    region_uuid_identifier: Optional[UUID]
    institution_identifier: str
    institution_uuid_identifier: Optional[UUID]
    departments: List[SDDepartment]
