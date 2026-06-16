# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Orlov

from ..conftest import VALIDITY
from .conftest import assert_absent
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $user_key: String!) {
  leave_update(
    input: {uuid: $uuid, user_key: $user_key, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_leave_lifecycle(
    server: None,
    org_unit_type_facet: UUID,
    org_unit_level_facet: UUID,
    engagement_type_facet: UUID,
    job_function_facet: UUID,
    leave_type_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    create_org_unit: Callable[[dict[str, Any]], Awaitable[str]],
    create_engagement: Callable[[dict[str, Any]], Awaitable[str]],
    create_leave: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    terminate: Callable[[str, str], Awaitable[None]],
    actual_state_db_session: Session,
) -> None:
    session = actual_state_db_session

    leave_type_uuid = await create_class(
        {
            "user_key": "leave",
            "name": "Leave",
            "facet_uuid": str(leave_type_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    engagement_type_uuid = await create_class(
        {
            "user_key": "eng_type",
            "name": "Eng Type",
            "facet_uuid": str(engagement_type_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    job_function_uuid = await create_class(
        {
            "user_key": "job_func",
            "name": "Job Func",
            "facet_uuid": str(job_function_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
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

    person_uuid = await create_person(
        {
            "cpr_number": "0707700000",
            "given_name": "Leave",
            "surname": "User",
            "user_key": "leave_user",
        }
    )
    unit_uuid = await create_org_unit(
        {
            "user_key": "leave_unit",
            "name": "Leave Unit",
            "org_unit_type": unit_type_uuid,
            "org_unit_level": level_uuid,
            "validity": VALIDITY,
        }
    )
    engagement_uuid = await create_engagement(
        {
            "user_key": "leave_eng",
            "person": person_uuid,
            "org_unit": unit_uuid,
            "engagement_type": engagement_type_uuid,
            "job_function": job_function_uuid,
            "validity": VALIDITY,
        }
    )

    def expected(uuid: str, bvn: str) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "bvn": bvn,
            "bruger_uuid": person_uuid,
            "orlovstype_uuid": leave_type_uuid,
            "orlovstype_titel": "Leave",
            "engagement_uuid": engagement_uuid,
            "startdato": "2020-01-01",
            "slutdato": "9999-12-31",
        }

    # Create
    uuid = await create_leave(
        {
            "user_key": "my_leave",
            "person": person_uuid,
            "leave_type": leave_type_uuid,
            "engagement": engagement_uuid,
            "validity": VALIDITY,
        }
    )
    await assert_row(session, Orlov, expected(uuid, "my_leave"))

    # Update
    await mutate(UPDATE, uuid=uuid, user_key="new_leave")
    await assert_row(session, Orlov, expected(uuid, "new_leave"))

    # Delete (leave has no _delete mutation; terminate removes it)
    await terminate("leave", uuid)
    await assert_absent(session, Orlov)
