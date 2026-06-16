# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of an employee.

Mirrors ``test_employee_sync`` but asserts the historic export DB. An employee
persists after termination, so it is hard-deleted, removing it from both
export targets.
"""

from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Bruger

from .conftest import assert_absent
from .conftest import assert_row
from .conftest import assert_rows

UPDATE = """
mutation ($uuid: UUID!, $surname: String!) {
  employee_update(
    input: {uuid: $uuid, surname: $surname, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


def expected(
    uuid: str, surname: str, startdato: str, slutdato: str = "9999-12-31"
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "bvn": "user_key",
        "fornavn": "given_name",
        "efternavn": surname,
        "kaldenavn_fornavn": "",
        "kaldenavn_efternavn": "",
        "cpr": "0101700000",
        "startdato": startdato,
        "slutdato": slutdato,
    }


@pytest.mark.integration_test
async def test_employee_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

    # Create: a single open period (employee created without validity).
    uuid = await create_person(
        {
            "cpr_number": "0101700000",
            "given_name": "given_name",
            "surname": "surname",
            "user_key": "user_key",
        }
    )
    await assert_row(session, Bruger, expected(uuid, "surname", "1970-01-01"))

    # Update: narrowing the validity to 2020-01-01 splits the entity into two
    # retained periods in the full-history export.
    await mutate(UPDATE, uuid=uuid, surname="new_surname")
    await assert_rows(
        session,
        Bruger,
        [
            # slutdato is the inclusive last valid day, i.e. the day before the
            # next period begins.
            expected(uuid, "surname", "1970-01-01", "2019-12-31"),
            expected(uuid, "new_surname", "2020-01-01"),
        ],
    )

    # Delete (an employee persists after termination, so hard-delete it)
    await delete("employee", uuid)
    await assert_absent(session, Bruger)
