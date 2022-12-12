import logging
import re
from datetime import date
from datetime import datetime
from datetime import timedelta
from itertools import chain
from itertools import takewhile
from typing import OrderedDict, Union
from typing import Iterator
from typing import Optional
from typing import Tuple

from more_itertools import pairwise
from more_itertools import tabulate

from .sd_common import EmploymentStatus

# TODO: move constants elsewhere
# TODO: set back to "infinity" when MO can handle this
# MO_INFINITY: str = "infinity"

MO_INFINITY = None
SD_INFINITY: str = "9999-12-31"
DATE_REGEX_STR = "[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[0-1])"

logger = logging.getLogger("sdImport")


def date_to_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def format_date(date_time: datetime) -> str:
    return date_time.strftime("%Y-%m-%d").zfill(10)


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def datetime_to_sd_date(date_time: Union[datetime, date]) -> str:
    return date_time.strftime("%d.%m.%Y")


def _get_employment_from_date(employment: OrderedDict) -> datetime:
    """
    Get the latest date of all the dates in the payload from the SD
    GetEmployment endpoint (see https://redmine.magenta-aps.dk/issues/51898)

    Args:
        employment: the SD employment

    Returns: the maximum of all the date in the SD payload

    """
    MIN_DATE = format_date(datetime.min)

    employment_date = employment.get("EmploymentDate", MIN_DATE)
    employment_department_date = employment.get("EmploymentDepartment", dict()).get(
        "ActivationDate", MIN_DATE
    )
    employment_status_date = employment.get("EmploymentStatus", dict()).get(
        "ActivationDate", MIN_DATE
    )
    profession_date = employment.get("Profession", dict()).get(
        "ActivationDate", MIN_DATE
    )
    working_time_date = employment.get("WorkingTime", dict()).get(
        "ActivationDate", MIN_DATE
    )

    return max(
        parse_date(employment_date),
        parse_date(employment_department_date),
        parse_date(employment_status_date),
        parse_date(profession_date),
        parse_date(working_time_date),
    )


def get_employment_dates(employment: OrderedDict) -> Tuple[datetime, datetime]:
    """
    Get the "from" and "to" date from the SD employment

    Args:
        employment: The SD employment

    Returns:
        Tuple containing the "from" and "to" dates.
    """

    status = EmploymentStatus(employment["EmploymentStatus"]["EmploymentStatusCode"])

    if status in EmploymentStatus.let_go():
        date_from = parse_date(employment["EmploymentDate"])
        termination_date = str(
            sd_to_mo_termination_date(employment["EmploymentStatus"]["ActivationDate"])
        )
        date_to = parse_date(termination_date)
        return date_from, date_to

    date_from = _get_employment_from_date(employment)
    date_to = parse_date(employment["EmploymentStatus"]["DeactivationDate"])

    return date_from, date_to


# TODO: Create "MoValidity" and "SdValidity" classes based on the RA Models
#  "Validity" class and use these as input to the function below


def sd_to_mo_termination_date(sd_date: str) -> Optional[str]:
    """
    Convert SD termination date to MO termination date.

    In MO, the termination date is the last day of work, while in SD it is the
    first day of non-work.

    Args:
        sd_date: SD termination date formatted as "YYYY-MM-DD"

    Returns:
        MO termination date formatted as "YYYY-MM-DD"
    """

    assert isinstance(sd_date, str)
    assert re.compile(DATE_REGEX_STR).match(sd_date)

    if sd_date == SD_INFINITY:
        return MO_INFINITY

    # In MO, the termination date is the last day of work,
    # in SD it is the first day of non-work.
    _sd_date = parse_date(sd_date)
    mo_date = _sd_date - timedelta(days=1)

    return format_date(mo_date)


def to_midnight(dt: datetime) -> datetime:
    """Get previous midnight from datetime."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def is_midnight(dt: datetime) -> bool:
    """Check if datetime is at midnight."""
    return dt == to_midnight(dt)


def gen_cut_dates(from_datetime: datetime, to_datetime: datetime) -> Iterator[datetime]:
    """Get iterator of cut-dates between from_datetime and to_datetime.

    Args:
        from_datetime: the start date
        to_datetime: the end date

    Yields:
        The from_datetime, then all intermediate midnight datetimes and the to_datetime.
    """
    assert from_datetime < to_datetime

    # Tabulate to infinite iterator of midnights starting after from_datetime
    def midnight_at_offset(offset: int) -> datetime:
        return to_midnight(from_datetime) + timedelta(days=offset)

    midnights = takewhile(
        lambda midnight: midnight < to_datetime, tabulate(midnight_at_offset, start=1)
    )
    return chain([from_datetime], midnights, [to_datetime])


def gen_date_intervals(
    from_datetime: datetime, to_datetime: datetime
) -> Iterator[Tuple[datetime, datetime]]:
    """
    Get iterator capable of generating a sequence of datetime pairs
    incrementing one day at a time. The latter date in a pair is
    advanced by exactly one day compared to the former date in the pair.

    Args:
        from_datetime: the start date
        to_datetime: the end date

    Yields:
        The next date pair in the sequence of pairs
    """
    return pairwise(gen_cut_dates(from_datetime, to_datetime))
