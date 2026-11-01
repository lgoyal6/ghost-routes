"""Integration fixtures: a throwaway database on the project's PostGIS server."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ghost_routes.config import get_settings
from ghost_routes.persistence import migrations

pytestmark = pytest.mark.integration


def alembic_cfg(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations.__path__[0]))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    base = make_url(os.environ.get("GHOST_ROUTES_DATABASE_URL", get_settings().database_url))
    name = f"gr_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        pytest.fail(f"PostGIS is required for integration tests (run `make dev`): {exc}")
    url = base.set(database=name).render_as_string(hide_password=False)
    command.upgrade(alembic_cfg(url), "head")
    yield url
    from ghost_routes.persistence.db import engine

    engine(url).dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def clean(db_url: str) -> str:
    eng = create_engine(db_url)
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename NOT IN ('alembic_version','spatial_ref_sys')"))]
        c.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER review_decisions_append_only"))
        c.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
        c.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER review_decisions_append_only"))
    eng.dispose()
    return db_url
