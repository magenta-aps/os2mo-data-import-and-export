# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Fixtures and helpers shared by the event-driven integration tests.

Each test drives a full lifecycle (create -> update -> delete) of one entity
type and asserts that the export DB stays in sync via AMQP events. The app is
started (``server`` fixture) so it consumes events; OS2mo emits them via the
``amqp_event_emitter`` fixture, and we poll the export DB with ``retry`` until
the expected state is reached.
"""

import os
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastramqpi.pytest_util import retry
from gql import gql
from gql.client import AsyncClientSession
from more_itertools import one
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from sql_export.main import create_app
from sql_export.sql_table_defs import Base

from ..conftest import sql_to_dict

# A validity ending well in the past, so the terminated entity is no longer
# "current" and is therefore removed from the export DB.
TERMINATE_TO = "2021-01-01"


@pytest.fixture
def app(load_marked_envvars: None) -> FastAPI:
    # Ensure EVENTDRIVEN is True so AMQP handlers are registered (and the
    # /trigger endpoint is not mounted). The app then syncs entities to the
    # export DB in response to AMQP events emitted by OS2mo.
    os.environ["EVENTDRIVEN"] = "true"
    return create_app()


def _engine(prefix: str):
    user = os.environ[f"{prefix}__USER"]
    password = os.environ[f"{prefix}__PASSWORD"]
    host = os.environ[f"{prefix}__HOST"]
    port = os.environ.get(f"{prefix}__PORT", "5432")
    name = os.environ[f"{prefix}__DB_NAME"]
    return create_engine(
        f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    )


@pytest.fixture
def historic_state_db_session() -> Iterator[Session]:
    """Session against the historic export DB (full-history export target)."""
    with Session(_engine("HISTORIC_STATE")) as session:
        yield session


@pytest.fixture
def purge_historic_export_db() -> Iterator[None]:
    """Truncate the historic export DB before a test that asserts on it."""
    engine = _engine("HISTORIC_STATE")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))
        session.commit()
    yield


@pytest.fixture
def mutate(
    graphql_client: AsyncClientSession,
) -> Callable[..., Awaitable[dict]]:
    """Execute an arbitrary GraphQL mutation, returning the response data."""

    async def inner(mutation: str, **variables: Any) -> dict:
        return await graphql_client.execute(  # type: ignore[misc]
            gql(mutation), variable_values=variables
        )

    return inner


@pytest.fixture
def delete(
    mutate: Callable[..., Awaitable[dict]],
) -> Callable[[str, str], Awaitable[None]]:
    """Hard-delete any entity by its MO type and UUID.

    All ``*_delete`` mutations take a single ``uuid`` argument. Use this for
    types where termination does not end the entity's existence (e.g. an
    employee persists after termination).
    """

    async def inner(mo_type: str, uuid: str) -> None:
        await mutate(
            f"mutation ($uuid: UUID!) {{  {mo_type}_delete(uuid: $uuid) {{ uuid }}}}",
            uuid=uuid,
        )

    return inner


@pytest.fixture
def terminate(
    mutate: Callable[..., Awaitable[dict]],
) -> Callable[[str, str], Awaitable[None]]:
    """Terminate any entity by its MO type and UUID.

    All ``*_terminate`` mutations accept the same ``{uuid, to}`` input, so we
    build the input inline and only need scalar variables - no per-type input
    type names.
    """

    async def inner(mo_type: str, uuid: str, to: str = TERMINATE_TO) -> None:
        await mutate(
            f"mutation ($uuid: UUID!, $to: DateTime!) {{"
            f"  {mo_type}_terminate(input: {{uuid: $uuid, to: $to}}) {{ uuid }}"
            f"}}",
            uuid=uuid,
            to=to,
        )

    return inner


async def assert_row(session: Session, model: type, expected: dict[str, Any]) -> None:
    """Poll until the single row of ``model`` matches ``expected``."""

    @retry()
    async def check() -> None:
        session.expire_all()
        assert sql_to_dict(one(session.query(model).all())) == expected

    await check()


async def assert_rows(
    session: Session, model: type, expected: list[dict[str, Any]]
) -> None:
    """Poll until the rows of ``model`` match ``expected`` (order-insensitive).

    Used by historic (full-history) tests where a single entity is exported as
    several rows, one per retained validity period.
    """

    @retry()
    async def check() -> None:
        session.expire_all()
        results: list[Any] = session.query(model).all()
        rows = [sql_to_dict(r) for r in results]
        assert sorted(rows, key=str) == sorted(expected, key=str)

    await check()


async def assert_absent(session: Session, model: type) -> None:
    """Poll until no rows of ``model`` exist."""

    @retry()
    async def check() -> None:
        session.expire_all()
        assert session.query(model).all() == []

    await check()
