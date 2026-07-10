import os
from typing import Any
from typing import AsyncIterator
from typing import Awaitable
from typing import Callable
from typing import Iterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastramqpi.raclients.graph.client import GraphQLClient
from gql import gql
from gql.client import AsyncClientSession
from httpx import AsyncClient
from pydantic import AnyHttpUrl
from pydantic import parse_obj_as
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sql_export.main import create_app

GRAPHQL_VERSION = 30
VALIDITY = {"from": "2020-01-01", "to": None}


def sql_to_dict(obj):
    """Convert a SQLAlchemy model instance to a dict of all column values."""
    return {
        c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name != "id"
    }


async def _trigger(test_client: AsyncClient, resolve_dar: bool) -> None:
    response = await test_client.post(
        "/trigger",
        params={
            "resolve_dar": resolve_dar,
            "historic": False,
            "read_from_cache": False,
        },
        timeout=60.0,
    )
    assert response.status_code == 200
    assert response.json() == {"detail": "Done"}


@pytest.fixture
def trigger(test_client: AsyncClient) -> Callable[[], Awaitable[None]]:
    async def inner() -> None:
        await _trigger(test_client, resolve_dar=False)

    return inner


@pytest.fixture
def trigger_with_dar(test_client: AsyncClient) -> Callable[[], Awaitable[None]]:
    async def inner() -> None:
        await _trigger(test_client, resolve_dar=True)

    return inner


@pytest.fixture
def app(load_marked_envvars: None) -> FastAPI:
    # Ensure EVENTDRIVEN is False so /trigger endpoint is mounted
    os.environ["EVENTDRIVEN"] = "false"
    return create_app()


@pytest.fixture
async def graphql_client(mo_client: AsyncClient) -> AsyncIterator[AsyncClientSession]:
    """Authenticated GraphQL codegen client for OS2mo."""
    url = f"{mo_client.base_url}/graphql/v{GRAPHQL_VERSION}"

    client = GraphQLClient(
        url=url,
        client_id=os.environ["CLIENT_ID"],
        client_secret=os.environ["CLIENT_SECRET"],
        auth_realm=os.environ["AUTH_REALM"],
        auth_server=parse_obj_as(AnyHttpUrl, os.environ["AUTH_SERVER"]),
    )
    async with client as c:
        yield c


@pytest.fixture
def actual_state_db_session() -> Iterator[Session]:
    # Connect to the actual state DB
    db_user = os.environ["ACTUAL_STATE__USER"]
    db_pass = os.environ["ACTUAL_STATE__PASSWORD"]
    db_host = os.environ["ACTUAL_STATE__HOST"]
    db_port = os.environ.get("ACTUAL_STATE__PORT", "5432")
    db_name = os.environ["ACTUAL_STATE__DB_NAME"]

    url = f"postgresql+psycopg2://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
    engine = create_engine(url)
    with Session(engine) as session:
        yield session


@pytest.fixture
def create_org(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an OrgUnit."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateOrg($input: OrganisationCreate!) {
            org_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["org_create"]["uuid"]

    return inner


@pytest.fixture
async def root_org(
    create_org: Callable[[dict[str, Any]], Awaitable[str]],
) -> str:
    """Create the root organisation required by all MO operations."""
    return await create_org({"municipality_code": None})


@pytest.fixture
def create_person(
    root_org: str,
    graphql_client: AsyncClientSession,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Person."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateEmployee($input: EmployeeCreateInput!) {
            employee_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["employee_create"]["uuid"]

    return inner


@pytest.fixture
def create_facet(
    root_org: str,
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Facet."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateFacet($input: FacetCreateInput!) {
            facet_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["facet_create"]["uuid"]

    return inner


@pytest.fixture
def create_class(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Class."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateClass($input: ClassCreateInput!) {
            class_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["class_create"]["uuid"]

    return inner


@pytest.fixture
def create_it_system(
    root_org: str,
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an ITSystem."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateITSystem($input: ITSystemCreateInput!) {
            itsystem_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["itsystem_create"]["uuid"]

    return inner


@pytest.fixture
def create_it_connection(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an ITConnection (ITUser)."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateITUser($input: ITUserCreateInput!) {
            ituser_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["ituser_create"]["uuid"]

    return inner


@pytest.fixture
async def org_unit_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "org_unit_type",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
async def org_unit_level_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "org_unit_level",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
def create_org_unit(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an OrgUnit."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateOrgUnit($input: OrganisationUnitCreateInput!) {
            org_unit_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["org_unit_create"]["uuid"]

    return inner


@pytest.fixture
async def kle_aspect_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {"user_key": "kle_aspect", "published": "Publiceret", "validity": VALIDITY}
        )
    )


@pytest.fixture
async def kle_number_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {"user_key": "kle_number", "published": "Publiceret", "validity": VALIDITY}
        )
    )


@pytest.fixture
def create_kle(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a KLE."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateKLE($input: KLECreateInput!) {
            kle_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["kle_create"]["uuid"]

    return inner


@pytest.fixture
def create_related(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Related Units (Enhedssammenkobling)."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateRelatedUnits($input: RelatedUnitsUpdateInput!) {
            related_units_update(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["related_units_update"]["uuid"]

    return inner


@pytest.fixture
async def address_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "address_type",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
async def visibility_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {"user_key": "visibility", "published": "Publiceret", "validity": VALIDITY}
        )
    )


@pytest.fixture
def create_address(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an Address."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateAddress($input: AddressCreateInput!) {
            address_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["address_create"]["uuid"]

    return inner


@pytest.fixture
async def association_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "association_type",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
def create_association(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an Association."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateAssociation($input: AssociationCreateInput!) {
            association_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["association_create"]["uuid"]

    return inner


@pytest.fixture
async def engagement_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "engagement_type",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
async def job_function_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "job_function",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
def create_engagement(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create an Engagement."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateEngagement($input: EngagementCreateInput!) {
            engagement_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["engagement_create"]["uuid"]

    return inner


@pytest.fixture
async def leave_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {"user_key": "leave_type", "published": "Publiceret", "validity": VALIDITY}
        )
    )


@pytest.fixture
def create_leave(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Leave."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateLeave($input: LeaveCreateInput!) {
            leave_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["leave_create"]["uuid"]

    return inner


@pytest.fixture
async def manager_type_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "manager_type",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
async def manager_level_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "manager_level",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
async def responsibility_facet(
    create_facet: Callable[[dict[str, Any]], Awaitable[str]],
) -> UUID:
    return UUID(
        await create_facet(
            {
                "user_key": "responsibility",
                "published": "Publiceret",
                "validity": VALIDITY,
            }
        )
    )


@pytest.fixture
def create_manager(
    graphql_client: GraphQLClient,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Returns a function to create a Manager."""

    async def inner(input_data: dict[str, Any]) -> str:
        create_mutation = gql("""
        mutation CreateManager($input: ManagerCreateInput!) {
            manager_create(input: $input) {
                uuid
            }
        }
        """)

        create_resp = await graphql_client.execute(  # type: ignore[misc]
            create_mutation, variable_values={"input": input_data}
        )
        return create_resp["manager_create"]["uuid"]

    return inner
