# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Klasse

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $name: String!, $facet_uuid: UUID!) {
  class_update(
    input: {uuid: $uuid, name: $name, user_key: "my_class",
            facet_uuid: $facet_uuid, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_class_lifecycle(
    server: None,
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    facet_uuid = await create_facet(
        {"user_key": "my_facet", "published": "Publiceret", "validity": VALIDITY}
    )

    def expected(uuid: str, titel: str) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "bvn": "my_class",
            "titel": titel,
            "facet_uuid": facet_uuid,
            "facet_bvn": "my_facet",
        }

    # Create
    uuid = await create_class(
        {
            "user_key": "my_class",
            "name": "My Class",
            "facet_uuid": facet_uuid,
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    await assert_row(session, Klasse, expected(uuid, "My Class"))

    # Update
    await mutate(UPDATE, uuid=uuid, name="New Class", facet_uuid=facet_uuid)
    await assert_row(session, Klasse, expected(uuid, "New Class"))

    # Delete
    await delete("class", uuid)
    await assert_absent(session, Klasse)
