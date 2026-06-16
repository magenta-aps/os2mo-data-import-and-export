# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Bruger

from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $surname: String!) {
  employee_update(
    input: {uuid: $uuid, surname: $surname, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


def expected(uuid: str, surname: str, startdato: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "bvn": "user_key",
        "fornavn": "given_name",
        "efternavn": surname,
        "kaldenavn_fornavn": "",
        "kaldenavn_efternavn": "",
        "cpr": "0101700000",
        # An employee is created without validity (startdato 1970-01-01).
        # Updating with an explicit validity narrows it to that from-date.
        "startdato": startdato,
        "slutdato": "9999-12-31",
    }


@pytest.mark.integration_test
async def test_employee_lifecycle(
    server: None,
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    # Create
    uuid = await create_person(
        {
            "cpr_number": "0101700000",
            "given_name": "given_name",
            "surname": "surname",
            "user_key": "user_key",
        }
    )
    await assert_row(session, Bruger, expected(uuid, "surname", "1970-01-01"))

    # Update
    await mutate(UPDATE, uuid=uuid, surname="new_surname")
    await assert_row(session, Bruger, expected(uuid, "new_surname", "2020-01-01"))

    # Delete (an employee persists after termination, so hard-delete it)
    await delete("employee", uuid)
    await assert_absent(session, Bruger)
