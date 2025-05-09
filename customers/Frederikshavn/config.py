from functools import lru_cache

from fastramqpi.ra_utils.job_settings import JobSettings


class EmployeePhoneBookSettings(JobSettings):
    # FTPS settings for Frederikshavn:
    ftps_url: str | None
    ftps_port: int | None
    ftps_user: str | None
    ftps_pass: str | None
    ftps_folder: str | None
    ftps_certificate: str | None

    # Settings for Employee Phonebook:
    sql_cell_phone_number_field: str | None = (
        "AD-Mobil"  # Desired cell phone type - "AD-Mobil".
    )
    sql_phone_number_field_list: list | None = [
        "AD-Telefonnummer",
        "Telefon",
    ]  # Desired phone type -
    # "AD-Telefonnummer" and "Telefon".
    sql_visibility_scope_field: str | None = (
        "SECRET"  # Exclude visibility scope of - "SECRET".
    )
    sql_visibility_title_field: str | None = (
        "Hemmelig"  # Exclude visibility scope of - "Hemmelig".
    )
    sql_excluded_organisation_units_user_key: str | None = (
        "1018136"  # Exclude certain organisation units.
    )
    sql_excluded_organisation_units_uuid: str | None = (
        "f11963f6-2df5-9642-f1e3-0983dad332f4"  # Exclude certain
    )
    # organisation units by uuid.


@lru_cache()
def get_employee_phone_book_settings(*args, **kwargs) -> EmployeePhoneBookSettings:
    return EmployeePhoneBookSettings(*args, **kwargs)
