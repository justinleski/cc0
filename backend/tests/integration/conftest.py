import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from socket import create_connection
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.integrations.object_store import create_s3_client

COMPOSE_HINT = "docker compose up -d postgres rustfs rustfs-init"
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _require_tcp(name: str, url: str, default_port: int) -> None:
    endpoint = urlsplit(url)
    host = endpoint.hostname
    port = endpoint.port or default_port
    try:
        with create_connection((host, port), timeout=2):
            pass
    except OSError:
        # Host and port only. A database URL also carries the password.
        pytest.fail(f"{name} is not reachable at {host}:{port}. Start it with: {COMPOSE_HINT}")


@pytest.fixture(scope="session")
def stack_settings() -> Settings:
    """Fail early and clearly when the local data services are down.

    Probing the ports separately keeps a stopped container from looking like bad
    credentials, a missing extension, or a missing bucket.
    """
    settings = Settings().model_copy(
        update={"cursor_signing_key": SecretStr(secrets.token_hex(32))}
    )
    _require_tcp("Postgres", settings.database_url, 5432)
    store = settings.object_store_endpoint
    _require_tcp("RustFS", store, 443 if urlsplit(store).scheme == "https" else 80)
    return settings


@pytest.fixture
def running_stack(stack_settings: Settings) -> Settings:
    return stack_settings


@dataclass(frozen=True)
class CatalogDatabase:
    """A throwaway database with the catalog migration applied."""

    name: str
    migration_url: str
    api_url: str
    worker_url: str


def _swap_database(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def catalog_database(stack_settings: Settings) -> Iterator[CatalogDatabase]:
    """Migrate a temporary database so tests never touch the operator's catalog.

    The three logins are cluster-wide, so the migration reuses the real roles
    and grants them CONNECT on this database only; dropping the database drops
    those grants with it. Nothing downgrades here, so the shared roles survive.
    """
    settings = stack_settings
    name = f"cc0_catalog_test_{uuid4().hex[:12]}"
    admin_url = _swap_database(settings.migration_database_url, "postgres")
    database = CatalogDatabase(
        name=name,
        migration_url=_swap_database(settings.migration_database_url, name),
        api_url=_swap_database(settings.database_url, name),
        worker_url=_swap_database(settings.worker_database_url, name),
    )

    with psycopg.connect(admin_url, autocommit=True, connect_timeout=2) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = database.migration_url
    try:
        command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
        yield database
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def test_bucket(stack_settings: Settings) -> Iterator[str]:
    """A throwaway bucket, so tests never write to or delete from the operator's.

    Object keys are content-addressed and the fixtures are deterministic, so a
    test sharing the real bucket would write, and then delete, the very keys a
    local `python -m app.worker ingest` run already points its assets at.
    """
    name = f"cc0-test-{uuid4().hex[:12]}"
    client = create_s3_client(stack_settings, timeout_seconds=5)
    client.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=name):
            for item in page.get("Contents", []):
                client.delete_object(Bucket=name, Key=item["Key"])
        client.delete_bucket(Bucket=name)
        client.close()
