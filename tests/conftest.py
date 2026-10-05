"""Shared fixtures. Tests marked `live` need MySQL 8 (DB_URL or DB_HOST); otherwise they are skipped.
Live tests use a connection whose work is ALWAYS rolled back, so they never change real data."""
from __future__ import annotations

import os

import pytest

HAS_DB = bool(os.getenv("DB_URL") or os.getenv("DB_HOST"))


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs a MySQL 8 database (DB_URL or DB_HOST)")


def pytest_collection_modifyitems(config, items):
    if HAS_DB:
        return
    skip = pytest.mark.skip(reason="no database configured (set DB_URL or DB_HOST)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def tests_do_not_write_into_project_data():
    """Guard: tests must use tmp_path, never the real data/ folders."""
    from app.config import DATA_DIR

    def snapshot():
        return {p for p in DATA_DIR.rglob("*") if p.is_file()}

    before = snapshot()
    yield
    leaked = snapshot() - before
    assert not leaked, f"tests wrote into the project data folder: {sorted(map(str, leaked))}"


@pytest.fixture(scope="session")
def engine():
    from app.database.connection import get_engine
    from app.database.schema import apply_schema

    eng = get_engine()
    apply_schema(eng)
    return eng


@pytest.fixture
def conn(engine):
    """A connection whose work is always rolled back."""
    with engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()
