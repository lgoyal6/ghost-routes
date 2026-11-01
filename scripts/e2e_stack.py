"""Isolated stack for browser tests: a throwaway database, the API and a worker.

Creates a fresh database on the project's PostGIS server, migrates and seeds
it, runs the API (GHOST_ROUTES_API_PORT, default 8078) and one worker, and
drops the database on exit. A stack killed before cleanup leaves its database
behind; the next start drops it. Browser-test review actions are therefore never
written to the developer's database. Playwright starts and stops this script.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from ghost_routes.config import get_settings
from tests.integration.conftest import alembic_cfg

CLI = str(Path(sys.executable).with_name("ghost-routes"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def drop_orphans(admin: Engine) -> list[str]:
    """Drop e2e databases whose owning stack process is gone (it may have been killed before cleanup)."""
    dropped = []
    with admin.connect() as c:
        names = [r[0] for r in c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'gr\\_e2e\\_%'"))]
        for name in names:
            parts = name.split("_")
            owner = int(parts[2]) if len(parts) == 4 and parts[2].isdigit() else None
            if owner is None or not _alive(owner):
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                dropped.append(name)
    return dropped


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    base = make_url(get_settings().database_url)
    name = f"gr_e2e_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    drop_orphans(admin)
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    state = tempfile.mkdtemp(prefix="gr-e2e-state-")
    env = {**os.environ, "GHOST_ROUTES_DATABASE_URL": url, "GHOST_ROUTES_STATE_DIR": state,
           "GHOST_ROUTES_API_PORT": os.environ.get("GHOST_ROUTES_API_PORT", "8078"),
           "GHOST_ROUTES_WEB_PORT": os.environ.get("GHOST_ROUTES_WEB_PORT", "5178")}
    procs: list[subprocess.Popen[bytes]] = []
    try:
        command.upgrade(alembic_cfg(url), "head")
        subprocess.run([CLI, "seed", "--namespace", "ashgrove"], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        procs.append(subprocess.Popen([CLI, "worker"], env=env))
        procs.append(subprocess.Popen([CLI, "serve"], env=env))
        signal.signal(signal.SIGTERM, _interrupt)
        procs[-1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        shutil.rmtree(state, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
