"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ghost_routes.config import get_settings

# One lock key serializes every change of a "head" pointer with result
# publication, so a run can never be published as current against moved inputs.
HEADS_LOCK_KEY = 0x6768_6F73  # "ghos"


@lru_cache
def engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True, future=True)


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(engine(url), expire_on_commit=False)


@contextmanager
def transaction(url: str | None = None) -> Iterator[Session]:
    s = session_factory(url)()
    try:
        with s.begin():
            yield s
    finally:
        s.close()


def lock_heads(s: Session) -> None:
    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": HEADS_LOCK_KEY})


def migrations_current(s: Session) -> tuple[bool, str | None]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from ghost_routes.persistence import migrations

    cfg = Config()
    cfg.set_main_option("script_location", str(migrations.__path__[0]))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    try:
        cur = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:  # no alembic_version table yet
        return False, None
    return cur == head, cur
