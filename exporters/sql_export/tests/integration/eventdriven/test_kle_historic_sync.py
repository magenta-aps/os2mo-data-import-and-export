# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of a KLE.

Mirrors ``test_kle_sync`` but asserts the historic export DB; after termination
into the past the now-closed period is retained.
"""

from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import KLE

from ..conftest import VALIDITY
from .conftest import TERMINATE_TO
from .conftest import assert_row

# Change the KLE number to a second class, observable as kle_nummer_*.
# kle_update replaces the object, so org_unit and aspects are re-sent.
UPDATE = """
mutation ($uuid: UUID!, $org_unit: UUID!, $kle_aspects: [UUID!]!, $kle_number: UUID!) {
  kle_update(
    input: {
      uuid: $uuid
      org_unit: $org_unit
      kle_aspects: $kle_aspects
      kle_number: $kle_number
      validity: {from: "2020-01-01"}
    }
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_kle_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    org_unit_type_facet: UUID,
    org_unit_level_facet: UUID,
    kle_aspect_facet: UUID,
    kle_number_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_org_unit: Callable[[dict[str, Any]], Awaitable[str]],
    create_kle: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    terminate: Callable[[str, str], Awaitable[None]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

    kle_aspect_uuid = await create_class(
        {
            "user_key": "aspect",
            "name": "Aspect",
            "facet_uuid": str(kle_aspect_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    kle_number_uuid = await create_class(
        {
            "user_key": "number",
            "name": "Number",
            "facet_uuid": str(kle_number_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    kle_number2_uuid = await create_class(
        {
            "user_key": "number2",
            "name": "Number 2",
            "facet_uuid": str(kle_number_facet),
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

    unit_uuid = await create_org_unit(
        {
            "user_key": "kle_unit",
            "name": "KLE Unit",
            "org_unit_type": unit_type_uuid,
            "org_unit_level": level_uuid,
            "validity": VALIDITY,
        }
    )

    def expected(
        uuid: str, number_uuid: str, number_titel: str, slutdato: str = "9999-12-31"
    ) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "enhed_uuid": unit_uuid,
            "kle_aspekt_uuid": kle_aspect_uuid,
            "kle_aspekt_titel": "Aspect",
            "kle_nummer_uuid": number_uuid,
            "kle_nummer_titel": number_titel,
            "startdato": "2020-01-01",
            "slutdato": slutdato,
        }

    # Create
    uuid = await create_kle(
        {
            "org_unit": unit_uuid,
            "kle_aspects": [kle_aspect_uuid],
            "kle_number": kle_number_uuid,
            "validity": VALIDITY,
        }
    )
    await assert_row(session, KLE, expected(uuid, kle_number_uuid, "Number"))

    # Update
    await mutate(
        UPDATE,
        uuid=uuid,
        org_unit=unit_uuid,
        kle_aspects=[kle_aspect_uuid],
        kle_number=kle_number2_uuid,
    )
    await assert_row(session, KLE, expected(uuid, kle_number2_uuid, "Number 2"))

    # Terminate into the past: the closed period is retained in historic.
    await terminate("kle", uuid)
    await assert_row(
        session, KLE, expected(uuid, kle_number2_uuid, "Number 2", TERMINATE_TO)
    )
