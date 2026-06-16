# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Historic (full-history) export of an address.

Mirrors ``test_address_sync`` but asserts the historic export DB. The create
and update steps behave identically; only termination differs: the now-closed
period is retained in historic (slutdato = TERMINATE_TO) instead of dropped.
"""

from typing import Any
from typing import Awaitable
from typing import Callable
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from sql_export.sql_table_defs import Adresse

from ..conftest import VALIDITY
from .conftest import TERMINATE_TO
from .conftest import assert_row

# address_update replaces the object, so the full payload must be re-sent.
UPDATE = """
mutation (
  $uuid: UUID!
  $value: String!
  $person: UUID!
  $address_type: UUID!
  $visibility: UUID!
) {
  address_update(
    input: {
      uuid: $uuid
      value: $value
      person: $person
      address_type: $address_type
      visibility: $visibility
      validity: {from: "2020-01-01"}
    }
  ) { uuid }
}
"""


@pytest.mark.integration_test
async def test_address_historic_lifecycle(
    server: None,
    purge_historic_export_db: None,
    address_type_facet: UUID,
    visibility_facet: UUID,
    create_class: Callable[[dict[str, Any]], Awaitable[str]],
    create_person: Callable[[dict[str, Any]], Awaitable[str]],
    create_address: Callable[[dict[str, Any]], Awaitable[str]],
    mutate: Callable[..., Awaitable[dict]],
    terminate: Callable[[str, str], Awaitable[None]],
    historic_state_db_session: Session,
) -> None:
    session = historic_state_db_session

    address_type_uuid = await create_class(
        {
            "user_key": "email",
            "name": "Email",
            "facet_uuid": str(address_type_facet),
            "scope": "EMAIL",
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )
    visibility_uuid = await create_class(
        {
            "user_key": "public",
            "name": "Public",
            "facet_uuid": str(visibility_facet),
            "published": "Publiceret",
            "validity": VALIDITY,
        }
    )

    person_uuid = await create_person(
        {
            "cpr_number": "0303700000",
            "given_name": "Addr",
            "surname": "User",
            "user_key": "addr_user",
        }
    )

    def expected(uuid: str, value: str, slutdato: str = "9999-12-31") -> dict[str, Any]:
        return {
            "uuid": uuid,
            "bvn": "my_address",
            "bruger_uuid": person_uuid,
            "enhed_uuid": None,
            "engagement_uuid": None,
            "ituser_uuid": None,
            "værdi": value,
            "dar_uuid": None,
            "adressetype_uuid": address_type_uuid,
            "adressetype_bvn": "email",
            "adressetype_scope": "E-mail",
            "adressetype_titel": "Email",
            "synlighed_uuid": visibility_uuid,
            "synlighed_scope": None,
            "synlighed_titel": "Public",
            "startdato": "2020-01-01",
            "slutdato": slutdato,
        }

    # Create
    uuid = await create_address(
        {
            "user_key": "my_address",
            "value": "test@example.com",
            "person": person_uuid,
            "address_type": address_type_uuid,
            "visibility": visibility_uuid,
            "validity": VALIDITY,
        }
    )
    await assert_row(session, Adresse, expected(uuid, "test@example.com"))

    # Update
    await mutate(
        UPDATE,
        uuid=uuid,
        value="new@example.com",
        person=person_uuid,
        address_type=address_type_uuid,
        visibility=visibility_uuid,
    )
    await assert_row(session, Adresse, expected(uuid, "new@example.com"))

    # Terminate into the past: the closed period is retained in historic.
    await terminate("address", uuid)
    await assert_row(session, Adresse, expected(uuid, "new@example.com", TERMINATE_TO))
