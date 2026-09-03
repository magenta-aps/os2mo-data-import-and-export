# SPDX-FileCopyrightText: Magenta ApS <https://magenta.dk>
# SPDX-License-Identifier: MPL-2.0
"""Fixtures shared by the event-driven integration tests."""

import os

import pytest
from fastapi import FastAPI

from sql_export.main import create_app


@pytest.fixture
def app(load_marked_envvars: None) -> FastAPI:
    # Ensure EVENTDRIVEN is True. This ensures the /events/{...} routes are registered
    os.environ["EVENTDRIVEN"] = "true"
    return create_app()
