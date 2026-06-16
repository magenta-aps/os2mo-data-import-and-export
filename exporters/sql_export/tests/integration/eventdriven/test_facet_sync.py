# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Facet

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $user_key: String!) {
  facet_update(
    input: {uuid: $uuid, user_key: $user_key, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_facet_lifecycle(
    server: None,
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    # Create
    uuid = await create_facet(
        {"user_key": "my_facet", "published": "Publiceret", "validity": VALIDITY}
    )
    await assert_row(session, Facet, {"uuid": uuid, "bvn": "my_facet"})

    # Update
    await mutate(UPDATE, uuid=uuid, user_key="new_facet")
    await assert_row(session, Facet, {"uuid": uuid, "bvn": "new_facet"})

    # Delete
    await delete("facet", uuid)
    await assert_absent(session, Facet)
