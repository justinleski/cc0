import importlib
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from app.core.config import Settings
from app.db import catalog, roles
from app.db.roles import ROLE_API, ROLE_MIGRATOR, ROLE_NAMES, ROLE_WORKER, ensure_login_sql

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REVISION_FILES = (
    "0001_enable_vector_and_roles.py",
    "0002_default_privileges.py",
    "0003_catalog.py",
)


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


def _isolate_role_names(monkeypatch: pytest.MonkeyPatch, suffix: str) -> tuple[str, str, str]:
    """Suffix cluster-wide roles without touching cc0_api, cc0_migrator, or cc0_worker.

    Revision modules bind ROLE_* when they are imported. Alembic loads them by
    file, and a second test in this process may already have imported them, so
    rebind those globals after the roles module is patched. 0001 and 0002 also
    read app.db.roles at call time for the copy Alembic executes itself.
    """
    # Import first when the process has not loaded the revisions yet, so the
    # rebind below covers the already-imported case a later test would hit.
    for module_name in (
        "migrations.versions.0001_enable_vector_and_roles",
        "migrations.versions.0002_default_privileges",
        "migrations.versions.0003_catalog",
    ):
        if module_name not in sys.modules:
            importlib.import_module(module_name)
    renamed = {
        name: f"{getattr(roles, name)}_{suffix}"
        for name in ("ROLE_MIGRATOR", "ROLE_API", "ROLE_WORKER")
    }
    for name, isolated in renamed.items():
        monkeypatch.setattr(roles, name, isolated)
        monkeypatch.setitem(globals(), name, isolated)
    isolated_roles = (renamed["ROLE_MIGRATOR"], renamed["ROLE_API"], renamed["ROLE_WORKER"])
    monkeypatch.setattr(roles, "ROLE_NAMES", isolated_roles)
    monkeypatch.setitem(globals(), "ROLE_NAMES", isolated_roles)
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if not path.endswith(_REVISION_FILES):
            continue
        try:
            importlib.reload(module)
        except (ImportError, AttributeError, ModuleNotFoundError):
            for name in ("ROLE_MIGRATOR", "ROLE_API", "ROLE_WORKER", "ROLE_NAMES"):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, getattr(roles, name))
    return isolated_roles


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
    isolated_roles = _isolate_role_names(monkeypatch, suffix)
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
            connection.execute(ensure_login_sql(ROLE_API, "must_not_replace_existing_password"))
        with psycopg.connect(
            _role_url(base, ROLE_API, passwords[ROLE_API], temp_database)
        ) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
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
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


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
    assert set(catalog.TABLES) < tables
    # Later milestones own these. The catalog revision must not create them.
    assert {"asset_embeddings", "boards", "board_items", "exports", "users"} & tables == set()
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

    _assert_no_default_privileges(url, base, database, passwords)
    _assert_catalog_grants(url)


def _assert_no_default_privileges(
    url: str,
    base: str,
    database: str,
    passwords: dict[str, str],
) -> None:
    """The M0 blanket grant is gone, for both migration creators.

    Leaving it in place would copy SELECT/INSERT/UPDATE/DELETE onto every table
    a later milestone adds, which is exactly the privilege question the catalog
    revision is supposed to answer per table.
    """
    for creator_url in (url, _role_url(base, ROLE_MIGRATOR, passwords[ROLE_MIGRATOR], database)):
        with psycopg.connect(creator_url) as connection:
            connection.execute("CREATE TABLE grants_probe (id serial PRIMARY KEY, value int)")
        try:
            for role in (ROLE_API, ROLE_WORKER):
                with psycopg.connect(
                    _role_url(base, role, passwords[role], database)
                ) as connection:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        connection.execute("SELECT value FROM grants_probe")
        finally:
            with psycopg.connect(creator_url) as connection:
                connection.execute("DROP TABLE grants_probe")


def _assert_catalog_grants(url: str) -> None:
    """Per-table privileges: the worker writes the catalog, the API reads two tables."""
    writable = ("SELECT", "INSERT", "UPDATE")
    with psycopg.connect(url) as connection:

        def granted(role: str, table: str, privilege: str) -> bool:
            return connection.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
            ).fetchone()[0]

        for table in catalog.TABLES:
            for privilege in writable:
                assert granted(ROLE_WORKER, table, privilege), f"{table}.{privilege}"
            # No DELETE anywhere: a dead job and its rights evidence have to
            # survive for an operator to inspect.
            assert not granted(ROLE_WORKER, table, "DELETE"), table

            readable = table in ("creators", "assets")
            assert granted(ROLE_API, table, "SELECT") is readable, table
            # The API cannot rewrite a rights decision or a job, and cannot see
            # providers, source records, evidence, or jobs at all.
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert not granted(ROLE_API, table, privilege), f"{table}.{privilege}"
