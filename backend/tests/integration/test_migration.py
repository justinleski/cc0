import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from app.core.config import Settings
from app.db import roles
from app.db.roles import ROLE_API, ROLE_MIGRATOR, ROLE_NAMES, ROLE_WORKER, ensure_login_sql

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _split_admin_url(url: str) -> tuple[str, str]:
    base, database = url.rsplit("/", 1)
    return base, database


def _upgrade(url: str) -> None:
    os.environ["MIGRATION_DATABASE_URL"] = url
    command.upgrade(_config(), "head")


def _downgrade(url: str) -> None:
    os.environ["MIGRATION_DATABASE_URL"] = url
    command.downgrade(_config(), "base")


def _role_url(base: str, role: str, password: str, database: str) -> str:
    host = base.split("@", 1)[1]
    return f"postgresql://{role}:{password}@{host}/{database}"


def test_migration_enables_vector_and_creates_three_roles(
    monkeypatch: pytest.MonkeyPatch,
    running_stack: Settings,
) -> None:
    settings = running_stack
    base, _ = _split_admin_url(settings.migration_database_url)
    suffix = uuid4().hex[:12]
    temp_database = f"cc0_migration_test_{suffix}"
    temp_url = f"{base}/{temp_database}"
    # Roles are cluster-wide: isolate both upgrade and downgrade from the app logins.
    for name in ("ROLE_API", "ROLE_MIGRATOR", "ROLE_WORKER"):
        isolated_role = f"{getattr(roles, name)}_{suffix}"
        monkeypatch.setattr(roles, name, isolated_role)
        monkeypatch.setitem(globals(), name, isolated_role)
    isolated_roles = (ROLE_MIGRATOR, ROLE_API, ROLE_WORKER)
    monkeypatch.setattr(roles, "ROLE_NAMES", isolated_roles)
    monkeypatch.setitem(globals(), "ROLE_NAMES", isolated_roles)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", temp_url)
    passwords = {
        ROLE_MIGRATOR: settings.cc0_migrator_password,
        ROLE_API: settings.cc0_api_password,
        ROLE_WORKER: settings.cc0_worker_password,
    }

    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=2) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(temp_database)))

    try:
        _upgrade(temp_url)
        _assert_migrated(temp_url, passwords)
        with psycopg.connect(temp_url, autocommit=True) as connection:
            connection.execute(ensure_login_sql(ROLE_API, passwords[ROLE_API]))
        _downgrade(temp_url)
        with psycopg.connect(temp_url) as connection:
            extension = connection.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            remaining = connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(ROLE_NAMES),),
            ).fetchall()
        assert extension is None
        assert remaining == []
    finally:
        with psycopg.connect(f"{base}/postgres", autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(temp_database)
                )
            )
            for role in isolated_roles:
                connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def _assert_migrated(url: str, passwords: dict[str, str]) -> None:
    base, database = _split_admin_url(url)
    with psycopg.connect(url) as connection:
        extension = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        roles = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole
                FROM pg_roles
                WHERE rolname = ANY(%s)
                """,
                (list(ROLE_NAMES),),
            )
        }

    assert extension is not None
    assert tables == {"alembic_version"}
    assert set(roles) == set(ROLE_NAMES)
    for can_login, is_super, can_create_db, can_create_role in roles.values():
        assert can_login is True
        assert is_super is False
        assert can_create_db is False
        assert can_create_role is False

    for role in (ROLE_API, ROLE_WORKER):
        with psycopg.connect(_role_url(base, role, passwords[role], database)) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE catalog_probe (id int)")

    with psycopg.connect(
        _role_url(base, ROLE_MIGRATOR, passwords[ROLE_MIGRATOR], database)
    ) as connection:
        connection.execute("CREATE TABLE migrator_probe (id int)")
        connection.execute("DROP TABLE migrator_probe")
