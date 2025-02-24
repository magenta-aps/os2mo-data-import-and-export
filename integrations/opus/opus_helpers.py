import datetime
import hashlib
import logging
import sqlite3
import uuid
from functools import lru_cache
from operator import itemgetter
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

import xmltodict
from deepdiff import DeepDiff
from fastramqpi.ra_utils.tqdm_wrapper import tqdm
from more_itertools import first
from more_itertools import partition

from integrations.opus.opus_exceptions import ImporterrunNotCompleted
from integrations.opus.opus_file_reader import get_opus_filereader

START_DATE = datetime.datetime(2019, 1, 1, 0, 0)

logger = logging.getLogger("opusHelper")


def read_available_dumps() -> Dict[datetime.datetime, str]:
    dumps = get_opus_filereader().list_opus_files()
    assert len(dumps) > 0, "No Opus files found!"
    return dumps


def get_latest_dump():
    dumps = read_available_dumps()
    latest_date = max(dumps.keys())
    return latest_date, dumps[latest_date]


def local_db_insert(run_db: Path, insert_tuple):
    conn = sqlite3.connect(
        run_db,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    c = conn.cursor()
    query = "insert into runs (dump_date, status) values (?, ?)"
    final_tuple = (insert_tuple[0], insert_tuple[1].format(datetime.datetime.now()))
    c.execute(query, final_tuple)
    conn.commit()
    conn.close()


def initialize_db(run_db):
    logger.info("Force is true, create new db")
    conn = sqlite3.connect(str(run_db))
    c = conn.cursor()
    c.execute(
        """
    CREATE TABLE runs (id INTEGER PRIMARY KEY,
    dump_date timestamp, status text)
    """
    )
    conn.commit()
    conn.close()


def next_xml_file(run_db, dumps) -> Tuple[Optional[datetime.date], datetime.date]:
    conn = sqlite3.connect(
        run_db,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    c = conn.cursor()
    query = "select * from runs order by id desc limit 1"
    c.execute(query)
    row = c.fetchone()
    latest_date = row[1]
    next_date = None
    if "Running" in row[2]:
        print("Critical error")
        logging.error("Previous run did not return!")
        raise ImporterrunNotCompleted("Previous run did not return!")

    for date in sorted(dumps.keys()):
        if date > latest_date:
            next_date = date
            break

    return next_date, latest_date


def parse_phone(phone_number):
    validated_phone = None
    if len(phone_number) == 8:
        validated_phone = phone_number
    elif len(phone_number) in (9, 11):
        validated_phone = phone_number.replace(" ", "")
    elif len(phone_number) in (4, 5):
        validated_phone = "0000" + phone_number.replace(" ", "")

    if validated_phone is None:
        logger.warning("Could not parse phone {}".format(phone_number))
    return validated_phone


@lru_cache(maxsize=None)
def generate_uuid(value, municipality_name: str):
    """
    Generate a predictable uuid based on org name and a unique value.
    """
    base_hash = hashlib.md5(municipality_name.encode())
    base_digest = base_hash.hexdigest()
    base_uuid = uuid.UUID(base_digest)

    combined_value = (str(base_uuid) + str(value)).encode()
    value_hash = hashlib.md5(combined_value)
    value_digest = value_hash.hexdigest()
    value_uuid = uuid.UUID(value_digest)
    return value_uuid


def parser(target_file: str, opus_id: Optional[int] = None) -> Tuple[List, List]:
    """Read an opus file and return units and employees"""
    text_input = get_opus_filereader().read_file(target_file)

    data = xmltodict.parse(text_input)
    data = data["kmd"]
    units = data.get("orgUnit", [])
    employees = data.get("employee", [])
    if opus_id is not None:
        employees = list(filter(lambda x: int(x["@id"]) == opus_id, employees))
        units = list(filter(lambda x: int(x["@id"]) == opus_id, units))
    return units, employees


def find_changes(
    before: List[Dict], after: List[Dict], disable_tqdm: bool = True
) -> List[Dict]:
    """Filter a list of dictionaries based on differences to another list of dictionaries
    Used to find changes to org_units and employees in opus files.
    Any registration in lastChanged is ignored here.
    Use disable_tqdm in tests etc.

    Returns: list of dictionaries from 'after' where there are changes from 'before'
    >>> a = [{"@id":1, "text":"unchanged", '@lastChanged': 'some day'}, {"@id":2, "text":"before", '@lastChanged':'today'}]
    >>> b = [{"@id":1, "text":"unchanged", '@lastChanged': 'another day'}, {"@id":2, "text":"after"}]
    >>> c = [{"@id":1, "text":"unchanged", '@lastChanged': 'another day'}]
    >>> find_changes(a, a, disable_tqdm=True)
    []
    >>> find_changes(a, b, disable_tqdm=True)
    [{'@id': 2, 'text': 'after'}]
    >>> find_changes(b, a, disable_tqdm=True)
    [{'@id': 2, 'text': 'before', '@lastChanged': 'today'}]
    >>> find_changes(a, c, disable_tqdm=True)
    []
    """
    old_ids = list(map(itemgetter("@id"), before))
    old_map = dict(zip(old_ids, before))
    changed_obj = []

    def find_changed(obj: Dict) -> bool:
        # New object
        if obj["@id"] not in old_ids:
            return True

        old_obj = old_map[obj["@id"]]
        diff = DeepDiff(
            obj,
            old_obj,
            exclude_paths={
                "root['@lastChanged']",
                "root['numerator']",
                "root['denominator']",
            },
        )
        # Changed object
        if diff:
            return True

        # Unchanged object
        return False

    after = tqdm(after, desc="Finding changes", disable=disable_tqdm)
    changed_obj = list(filter(find_changed, after))

    return changed_obj


def find_missing(before: List[Dict], after: List[Dict]) -> List[Dict]:
    """Check if an element is missing. This happens when an object is cancled in Opus.

    >>> a = [{"@id":1}, {"@id":2}, {"@id":3}]
    >>> b = [{"@id":1}, {"@id":3}]
    >>> find_missing(a,b)
    [{'@id': 2}]
    >>> find_missing(b,a)
    []
    """

    old_ids = set(map(itemgetter("@id"), before))
    new_ids = set(map(itemgetter("@id"), after))
    missing = old_ids - new_ids
    missing_elements = filter(lambda x: x.get("@id") in missing, before)
    return list(missing_elements)


def file_diff(
    file1: Optional[str],
    file2: str,
    disable_tqdm: bool = True,
    opus_id: Optional[int] = None,
):
    """Compares two files and returns all units and employees that have been changed."""
    units1: List[Dict] = []
    employees1: List[Dict] = []
    if file1:
        units1, employees1 = parser(file1, opus_id=opus_id)
    units2, employees2 = parser(file2, opus_id=opus_id)

    units = find_changes(units1, units2, disable_tqdm=disable_tqdm)
    cancelled_units = find_missing(units1, units2)

    employees = find_changes(employees1, employees2, disable_tqdm=disable_tqdm)
    cancelled_employees = find_missing(employees1, employees2)

    return {
        "units": units,
        "employees": employees,
        "cancelled_units": cancelled_units,
        "cancelled_employees": cancelled_employees,
    }


def compare_employees(original, new, force=False):
    """Differences is these keys will not be counted as a difference, unless force
    is set to true. Notice lastChanged is included here, since we perform a
    brute-force comparison and does not care for lastChanged.
    """
    skip_keys = [
        "productionNumber",
        "entryIntoGroup",
        "invoiceRecipient",
        "@lastChanged",
        "cpr",
    ]
    identical = True
    for key in new.keys():
        if key in skip_keys and not force:
            continue
        if not original.get(key) == new[key]:
            identical = False
            msg = "Changed {} from {} to {}"
            print(msg.format(key, original.get(key), new[key]))
    return identical


def filter_units(units, filter_ids):
    """Splits units into two based on filter_ids.

    Partitions the units such that no unit with a parent-id in filter_ids exist in one list.
    Any unit filtered like that is put in the other list.

    Example:
        >>> units = [(1, None), (2, 1), (3, 1), (4, 2), (5, 2), (6, 3), (7, 5)]
        >>> tup_to_unit = lambda tup: {'@id': tup[0], 'parentOrgUnit': tup[1]}
        >>> units = list(map(tup_to_unit, units))
        >>> get_ids = lambda units: list(map(itemgetter('@id'), units))
        >>> a, b = filter_units(units, [1])
        >>> get_ids(a)
        [1, 2, 3, 4, 5, 6, 7]
        >>> get_ids(b)
        []
        >>> a, b = filter_units(units, [2])
        >>> get_ids(a)
        [2, 4, 5, 7]
        >>> get_ids(b)
        [1, 3, 6]
        >>> a, b = filter_units(units, [3])
        >>> get_ids(a)
        [3, 6]
        >>> get_ids(b)
        [1, 2, 4, 5, 7]
        >>> a, b = filter_units(units, [3, 5])
        >>> get_ids(a)
        [3, 5, 6, 7]
        >>> get_ids(b)
        [1, 2, 4]
        >>> a, b = filter_units(units, [3, 7])
        >>> get_ids(a)
        [3, 6, 7]
        >>> get_ids(b)
        [1, 2, 4, 5]

    Args:
        units: List of units
        filter_ids: List of unit IDs to filter parents on

    Returns:
        list: List of units, with some filtered out
    """

    def get_parent(parent_map, entry):
        """Build a list of parents."""
        parent = parent_map.get(entry, None)
        if parent is None:
            return [entry]
        return [entry] + get_parent(parent_map, parent)

    parent_map = dict(map(itemgetter("@id", "parentOrgUnit"), units))
    filter_set = set(filter_ids)

    def is_disjoint_from_filter_ids(unit):
        """Test for overlap between parents and filter_set."""
        parent_set = set(get_parent(parent_map, unit["@id"]))
        return parent_set.isdisjoint(filter_set)

    return partition(is_disjoint_from_filter_ids, units)


def filter_employees(employees: Iterable[Dict], all_filtered_ids: set):
    """Remove any employees that has an engagement in an unit that is in all_filtered_ids

    >>> e = [{'orgUnit': "1"}, {'orgUnit': "2"}]
    >>> ids = {"2", "3"}
    >>> list(filter_employees(e, ids))
    [{'orgUnit': '1'}]

    """
    return filter(lambda empl: empl.get("orgUnit") not in all_filtered_ids, employees)


def split_employees_leaves(employees: List[Dict]) -> Tuple[Iterable, Iterable]:
    """Split list of employees into two iterables, with either active employees or terminated employees

    >>> e = [{'@action': "test"}, {'@action': "leave"}]
    >>> e1, e2 = split_employees_leaves(e)
    >>> list(e1)
    [{'@action': 'test'}]

    >>> list(e2)
    [{'@action': 'leave'}]
    """

    return partition(lambda empl: empl.get("@action") == "leave", employees)


def read_cpr(employee: dict) -> str:
    cpr = employee.get("cpr")
    if isinstance(cpr, dict):
        cpr = employee["cpr"]["#text"]
    elif isinstance(cpr, str):
        assert isinstance(int(cpr), int)
    else:
        raise TypeError("Can't read cpr in this format")
    return cpr


def find_all_filtered_units(inputfile, filter_ids) -> list[dict]:
    file_diffs = file_diff(None, inputfile)
    all_units = file_diffs["units"]
    all_units.extend(file_diffs["cancelled_units"])
    all_filtered_units, _ = filter_units(all_units, filter_ids)
    return list(all_filtered_units)


def read_and_transform_data(
    inputfile1: Optional[str],
    inputfile2: str,
    filter_ids: List[str],
    disable_tqdm=False,
    opus_id: Optional[int] = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """Gets the diff of two files and transforms the data based on filter_ids
    Returns the active units, filtered units, active employees which are not in a filtered unit,
    terminated employees and canceled employees
    """
    file_diffs = file_diff(
        inputfile1, inputfile2, disable_tqdm=disable_tqdm, opus_id=opus_id
    )

    employees = file_diffs["employees"]

    all_filtered_units = find_all_filtered_units(inputfile2, filter_ids)
    _, units = filter_units(file_diffs["units"], filter_ids)
    active_employees, terminated_employees = split_employees_leaves(employees)
    filtered_employees = filter_employees(
        active_employees, {unit["@id"] for unit in all_filtered_units}
    )
    return (
        list(units),
        list(all_filtered_units),
        list(filtered_employees),
        list(terminated_employees),
        file_diffs["cancelled_employees"],
    )


@lru_cache
def find_opus_root_unit_id() -> str:
    """Finds the opus id for the root opus unit.

    Reads the first available opus file and generates the uuid for the first unit in the file.
    Assumes this is the root organisation of opus.
    """
    dumps = read_available_dumps()

    first_date = min(sorted(dumps.keys()))
    units, _ = parser(dumps[first_date])
    main_unit = first(units)
    return main_unit["@id"]
