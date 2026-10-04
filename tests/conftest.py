"""Shared fixtures. Tenancy tests need a real PostgreSQL -- SQLite has no schemas."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql+psycopg://tenants:tenants@127.0.0.1:55432/tenants"
DATABASE_URL = os.environ.get("FLASK_TENANTS_TEST_DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(DATABASE_URL, future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure should skip
        pytest.skip(f"PostgreSQL not reachable at {DATABASE_URL}: {exc}")
    yield eng
    eng.dispose()
