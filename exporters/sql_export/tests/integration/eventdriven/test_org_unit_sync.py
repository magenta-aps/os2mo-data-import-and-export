# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Enhed

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $name: String!) {
  org_unit_update(
    input: {uuid: $uuid, name: $name, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_org_unit_lifecycle(
    server: None,
    org_unit_type_facet: UUID,
    org_unit_level_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_org_unit: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    delete: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    unit_type_uuid = await create_class(
        {
            "user_key": "unit_type",
            "name": "Unit Type",
            "facet_uuid": str(org_unit_type_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    level_uuid = await create_class(
        {
            "user_key": "level",
            "name": "Level",
            "facet_uuid": str(org_unit_level_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )

    def expected(uuid: str, name: str) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "navn": name,
            "bvn": "my_unit",
            "forældreenhed_uuid": None,
            "enhedstype_uuid": unit_type_uuid,
            "enhedstype_titel": "Unit Type",
            "enhedsniveau_uuid": level_uuid,
            "enhedsniveau_titel": "Level",
            "tidsregistrering_uuid": None,
            "tidsregistrering_titel": "",
            # For a root unit the path equals its own name.
            "organisatorisk_sti": name,
            "leder_uuid": None,
            "fungerende_leder_uuid": None,
            "opmærkning_uuid": None,
            "opmærkning_titel": None,
            "startdato": "2020-01-01",
            "slutdato": "9999-12-31",
        }

    # Create
    uuid = await create_org_unit(
        {
            "user_key": "my_unit",
            "name": "My Unit",
            "org_unit_type": unit_type_uuid,
            "org_unit_level": level_uuid,
            "validity": VALIDITY,
        }
    )
    await assert_row(session, Enhed, expected(uuid, "My Unit"))

    # Update
    await mutate(UPDATE, uuid=uuid, name="New Unit")
    await assert_row(session, Enhed, expected(uuid, "New Unit"))

    # Delete
    await delete("org_unit", uuid)
    await assert_absent(session, Enhed)
