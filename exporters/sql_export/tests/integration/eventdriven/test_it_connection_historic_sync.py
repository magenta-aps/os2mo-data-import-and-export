# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of an IT connection.

Mirrors ``test_it_connection_sync`` but asserts the historic export DB; after
termination into the past the now-closed period is retained.
"""

from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import ItForbindelse

from ..conftest import VALIDITY
from .conftest import TERMINATE_TO
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $user_key: String!) {
  ituser_update(
    input: {uuid: $uuid, user_key: $user_key, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


def expected(
    uuid: str,
    it_system_uuid: str,
    person_uuid: str,
    brugernavn: str,
    slutdato: str = "9999-12-31",
) -> dict:
    return {
        "uuid": uuid,
        "it_system_uuid": it_system_uuid,
        "bruger_uuid": person_uuid,
        "enhed_uuid": None,
        "brugernavn": brugernavn,
        "eksternt_id": None,
        "primær_boolean": None,
        "startdato": "2020-01-01",
        "slutdato": slutdato,
    }


@pytest.mark.integration_test
async def test_it_connection_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    create_it_system: Callable[[dict[str, Any]], Awaitable[str]],
    create_it_connection: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    terminate: Callable[[str, str], Awaitable[None]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

    person_uuid = await create_person(
        {
            "cpr_number": "0404700000",
            "given_name": "IT",
            "surname": "User",
            "user_key": "it_user",
        }
    )
    it_system_uuid = await create_it_system(
        {"name": "My System", "user_key": "my_system", "validity": VALIDITY}
    )

    # Create
    uuid = await create_it_connection(
        {
            "user_key": "it_username",
            "person": person_uuid,
            "itsystem": it_system_uuid,
            "validity": VALIDITY,
        }
    )
    await assert_row(
        session,
        ItForbindelse,
        expected(uuid, it_system_uuid, person_uuid, "it_username"),
    )

    # Update
    await mutate(UPDATE, uuid=uuid, user_key="new_username")
    await assert_row(
        session,
        ItForbindelse,
        expected(uuid, it_system_uuid, person_uuid, "new_username"),
    )

    # Terminate into the past: the closed period is retained in historic.
    await terminate("ituser", uuid)
    await assert_row(
        session,
        ItForbindelse,
        expected(uuid, it_system_uuid, person_uuid, "new_username", TERMINATE_TO),
    )
