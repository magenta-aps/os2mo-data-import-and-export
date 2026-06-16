# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import ItSystem

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $name: String!) {
  itsystem_update(
    input: {uuid: $uuid, name: $name, user_key: "my_system", validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_it_system_lifecycle(
    server: None,
    create_it_system: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    # Create
    uuid = await create_it_system(
        {"name": "My System", "user_key": "my_system", "validity": VALIDITY}
    )
    await assert_row(session, ItSystem, {"uuid": uuid, "navn": "My System"})

    # Update
    await mutate(UPDATE, uuid=uuid, name="New System")
    await assert_row(session, ItSystem, {"uuid": uuid, "navn": "New System"})

    # Delete
    await delete("itsystem", uuid)
    await assert_absent(session, ItSystem)
