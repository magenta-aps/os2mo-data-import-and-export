# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of related units.

Mirrors ``test_related_units_sync`` but asserts the historic export DB.
Clearing the relation re-points the same period (from 2020-01-01), leaving no
retained period, so the relation is absent in historic too.
"""

from typing import Any
from typing import Awaitable
from typing import Callable
from unittest.mock import ANY
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Enhedssammenkobling

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row


@pytest.mark.integration_test
async def test_related_units_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    org_unit_type_facet: UUID,
    org_unit_level_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_org_unit: Callable[[dict[str, Any]], Awaitable[str]],
    create_related: Callable[[dict[str, Any]], Awaitable[str]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

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

    async def make_unit(user_key: str, name: str) -> str:
        return await create_org_unit(
            {
                "user_key": user_key,
                "name": name,
                "org_unit_type": unit_type_uuid,
                "org_unit_level": level_uuid,
                "validity": VALIDITY,
            }
        )

    unit1_uuid = await make_unit("unit_1", "Unit 1")
    unit2_uuid = await make_unit("unit_2", "Unit 2")
    unit3_uuid = await make_unit("unit_3", "Unit 3")

    def expected(*units: str) -> dict[str, Any]:
        # org_unit_uuids from MO GraphQL is sorted, so enhed1 < enhed2.
        enhed1, enhed2 = sorted(units)
        return {
            "uuid": ANY,
            "enhed1_uuid": enhed1,
            "enhed2_uuid": enhed2,
            "startdato": "2020-01-01",
            "slutdato": "9999-12-31",
        }

    # Create: relate unit1 <-> unit2
    await create_related(
        {"origin": unit1_uuid, "destination": [unit2_uuid], "validity": VALIDITY}
    )
    await assert_row(session, Enhedssammenkobling, expected(unit1_uuid, unit2_uuid))

    # Update: re-point unit1 <-> unit3
    await create_related(
        {"origin": unit1_uuid, "destination": [unit3_uuid], "validity": VALIDITY}
    )
    await assert_row(session, Enhedssammenkobling, expected(unit1_uuid, unit3_uuid))

    # Delete: clear all relations for unit1
    await create_related(
        {"origin": unit1_uuid, "destination": [], "validity": VALIDITY}
    )
    await assert_absent(session, Enhedssammenkobling)
