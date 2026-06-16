# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of an engagement.

Mirrors ``test_engagement_sync`` but asserts the historic export DB; after
termination into the past the now-closed period is retained.
"""

from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Engagement

from ..conftest import VALIDITY
from .conftest import TERMINATE_TO
from .conftest import assert_row

UPDATE = """
mutation ($uuid: UUID!, $fraction: Int!) {
  engagement_update(
    input: {uuid: $uuid, fraction: $fraction, validity: {from: "2020-01-01"}}
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_engagement_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    org_unit_type_facet: UUID,
    org_unit_level_facet: UUID,
    engagement_type_facet: UUID,
    job_function_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    create_org_unit: Callable[[dict[str, Any]], Awaitable[str]],
    create_engagement: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    terminate: Callable[[str, str], Awaitable[None]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

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
            "cpr_number": "0202700000",
            "given_name": "Eng",
            "surname": "User",
            "user_key": "eng_user",
        }
    )
    unit_uuid = await create_org_unit(
        {
            "user_key": "eng_unit",
            "name": "Eng Unit",
            "org_unit_type": unit_type_uuid,
            "org_unit_level": level_uuid,
            "validity": VALIDITY,
        }
    )

    def expected(
        uuid: str, fraction: int, slutdato: str = "9999-12-31"
    ) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "bruger_uuid": person_uuid,
            "enhed_uuid": unit_uuid,
            "bvn": "my_eng",
            "arbejdstidsfraktion": fraction,
            "engagementstype_uuid": engagement_type_uuid,
            "engagementstype_titel": "Eng Type",
            "primærtype_uuid": None,
            "primærtype_titel": "",
            "stillingsbetegnelse_uuid": job_function_uuid,
            "stillingsbetegnelse_titel": "Job Func",
            "primær_boolean": False,
            "udvidelse_1": None,
            "udvidelse_2": None,
            "udvidelse_3": None,
            "udvidelse_4": None,
            "udvidelse_5": None,
            "udvidelse_6": None,
            "udvidelse_7": None,
            "udvidelse_8": None,
            "udvidelse_9": None,
            "udvidelse_10": None,
            "startdato": "2020-01-01",
            "slutdato": slutdato,
        }

    # Create
    uuid = await create_engagement(
        {
            "user_key": "my_eng",
            "person": person_uuid,
            "org_unit": unit_uuid,
            "engagement_type": engagement_type_uuid,
            "job_function": job_function_uuid,
            "fraction": 100,
            "validity": VALIDITY,
        }
    )
    await assert_row(session, Engagement, expected(uuid, 100))

    # Update
    await mutate(UPDATE, uuid=uuid, fraction=50)
    await assert_row(session, Engagement, expected(uuid, 50))

    # Terminate into the past: the closed period is retained in historic.
    await terminate("engagement", uuid)
    await assert_row(session, Engagement, expected(uuid, 50, TERMINATE_TO))
