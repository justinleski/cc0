import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[3]

# Compose interpolates these names and the API does not. They are allowed in
# `.env` and in the process environment even though they are not Settings fields.
COMPOSE_ALLOWLIST = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PORT",
        "RUSTFS_ACCESS_KEY",
        "RUSTFS_SECRET_KEY",
        "RUSTFS_API_PORT",
        "RUSTFS_CONSOLE_PORT",
        "S3_BUCKET",
        "API_PORT",
    }
)

# Not a Settings field. Passwords for the three application logins live in
# DATABASE_URL, MIGRATION_DATABASE_URL, WORKER_DATABASE_URL, and the CC0_*_PASSWORD
# fields. An exported DATABASE_PASSWORD is a common ambient name (it is not
# POSTGRES_PASSWORD and the API does not read it). Ignoring this exact name keeps
# that export from failing startup. A typo such as DATABASE_URLL is still rejected.
IGNORED_AMBIENT_ENV = frozenset({"DATABASE_PASSWORD"})

# Fail closed for names that look like application configuration. Compared
# case-insensitively: a variable is rejected when it is not a Settings field,
# not on COMPOSE_ALLOWLIST, not in IGNORED_AMBIENT_ENV, and it starts with one
# of these prefixes. `DATABASE` has no trailing underscore so `DATABASE_URL`
# and `DATABASE_URLL` both match. Unrelated names (PATH, HOME, GITHUB_*, npm)
# do not match and stay allowed. This is what catches a process-environment typo
# such as OBJECT_STORE_ENDPOIN, which extra="ignore" would otherwise drop,
# leaving the localhost default in place. Only names actually present are considered.
CONFIG_ENV_PREFIXES = (
    "APP_",
    "DATABASE",
    "MIGRATION_",
    "WORKER_",
    "CC0_",
    "OBJECT_STORE_",
    "READINESS_",
    "INGEST_",
    "POSTGRES_",
    "RUSTFS_",
    "S3_",
    "API_",
)

_DUMMY_SECRETS = frozenset(
    {
        "cc0_local_password",
        "cc0_migrator_local_password",
        "cc0_api_local_password",
        "cc0_worker_local_password",
        "cc0rustfs",
        "cc0rustfs_local_password",
    }
)

_LOCAL_OBJECT_STORE_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _allowed_config_names() -> set[str]:
    return (
        {name.upper() for name in Settings.model_fields}
        | set(COMPOSE_ALLOWLIST)
        | set(IGNORED_AMBIENT_ENV)
    )


def _is_project_config_name(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in CONFIG_ENV_PREFIXES)


def _unknown_process_env_keys() -> list[str]:
    allowed = _allowed_config_names()
    unknown = [
        key
        for key in os.environ
        if key.upper() not in allowed and _is_project_config_name(key)
    ]
    return sorted(unknown)


class Settings(BaseSettings):
    """Process configuration. Secrets stay in the environment and are not logged."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values):
        env_file = values.get("_env_file", self.model_config["env_file"])
        files = [env_file] if isinstance(env_file, (str, Path)) else (env_file or [])
        allowed = _allowed_config_names()
        for file in files:
            unknown = {key for key in dotenv_values(file) if key.upper() not in allowed}
            if unknown:
                raise ValueError("Unknown .env keys: " + ", ".join(sorted(unknown)))
        unknown_env = _unknown_process_env_keys()
        if unknown_env:
            raise ValueError("Unknown environment keys: " + ", ".join(unknown_env))
        super().__init__(**values)
        # After pydantic builds the model. Raising here avoids ValidationError,
        # whose payload is the whole settings dict and would print secrets.
        self._reject_nonlocal_defaults()

    def _reject_nonlocal_defaults(self) -> None:
        # "Local", " local ", and "LOCAL" are local. Anything else is fail-closed.
        if self.app_env.strip().lower() == "local":
            return
        secrets = [
            self.cc0_migrator_password,
            self.cc0_api_password,
            self.cc0_worker_password,
            self.object_store_access_key,
            self.object_store_secret_key,
        ]
        for url in (
            self.database_url,
            self.worker_database_url,
            self.migration_database_url,
        ):
            password = make_url(url).password
            if not password:
                raise ValueError(
                    "Non-local environments require a password in every database URL"
                )
            secrets.append(password)
        # Exact equality: a real secret that merely contains a dummy token is fine.
        if any(secret in _DUMMY_SECRETS for secret in secrets):
            raise ValueError("Non-local environments must replace all dummy credentials")
        host = (urlsplit(self.object_store_endpoint).hostname or "").lower()
        if host in _LOCAL_OBJECT_STORE_HOSTS:
            raise ValueError(
                "Non-local environments require a non-local object store endpoint"
            )

    app_env: str = "local"
    migration_database_url: str = Field(
        default="postgresql://cc0:cc0_local_password@127.0.0.1:5432/cc0",
    )
    database_url: str = Field(
        default="postgresql://cc0_api:cc0_api_local_password@127.0.0.1:5432/cc0",
    )
    worker_database_url: str = Field(
        default="postgresql://cc0_worker:cc0_worker_local_password@127.0.0.1:5432/cc0",
    )
    cc0_migrator_password: str = "cc0_migrator_local_password"
    cc0_api_password: str = "cc0_api_local_password"
    cc0_worker_password: str = "cc0_worker_local_password"
    object_store_endpoint: str = "http://127.0.0.1:9000"
    object_store_bucket: str = "cc0"
    object_store_access_key: str = "cc0rustfs"
    object_store_secret_key: str = "cc0rustfs_local_password"
    object_store_region: str = "us-east-1"
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    # Ingestion. Protective limits, not provider claims: the defaults are the
    # pilot values from the spec and an operator may only lower them safely.
    # `fixture` replays recorded responses and opens no socket, which is why
    # tests and CI need no network or API key. `network` is opt-in.
    ingest_source_mode: Literal["fixture", "network"] = "fixture"
    ingest_fixture_dir: str = "backend/fixtures/met"
    ingest_batch_limit: int = Field(default=100, gt=0)
    ingest_rate_limit_rps: float = Field(default=1.0, gt=0)
    ingest_request_timeout_seconds: float = Field(default=10.0, gt=0)
    ingest_max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    ingest_max_redirects: int = Field(default=3, ge=0)
    ingest_max_image_pixels: int = Field(default=50_000_000, gt=0)
    ingest_thumbnail_max_pixels: int = Field(default=512, gt=0)
    # One initial attempt plus three retries.
    ingest_max_attempts: int = Field(default=4, gt=0)
    ingest_backoff_base_seconds: float = Field(default=2.0, gt=0)
    ingest_backoff_max_seconds: float = Field(default=300.0, gt=0)
    ingest_lease_seconds: int = Field(default=300, gt=0)
    # Negative-cache recheck policy. Unknown is revisited sooner than a firm
    # non-CC0 answer because it is the status most likely to be wrong.
    ingest_recheck_unknown_days: int = Field(default=7, gt=0)
    ingest_recheck_non_cc0_days: int = Field(default=30, gt=0)
    # Comma-separated host allowlists. Metadata and image bytes are separate:
    # only an explicitly verified CC0 record ever reaches an image host.
    ingest_metadata_hosts: str = "collectionapi.metmuseum.org"
    ingest_image_hosts: str = "images.metmuseum.org"

    @property
    def fixture_dir(self) -> Path:
        path = Path(self.ingest_fixture_dir)
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def metadata_host_allowlist(self) -> frozenset[str]:
        return _host_set(self.ingest_metadata_hosts)

    @property
    def image_host_allowlist(self) -> frozenset[str]:
        return _host_set(self.ingest_image_hosts)


def _host_set(value: str) -> frozenset[str]:
    return frozenset(host.strip().lower() for host in value.split(",") if host.strip())


@lru_cache
def get_settings() -> Settings:
    """Load at process entry only; requests use app.state.settings and resources."""
    return Settings()
