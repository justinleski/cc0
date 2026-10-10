import os
from functools import lru_cache
from pathlib import Path
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

# Fail closed for names that look like application configuration. Compared
# case-insensitively: a variable is rejected when it is not a Settings field,
# not on COMPOSE_ALLOWLIST, and it starts with one of these prefixes.
# `DATABASE` has no trailing underscore so `DATABASE_URL` and `DATABASE_URLL`
# both match. Unrelated names (PATH, HOME, GITHUB_*, npm) do not match and stay
# allowed. This is what catches a process-environment typo such as
# OBJECT_STORE_ENDPOIN, which extra="ignore" would otherwise drop, leaving the
# localhost default in place. Only names actually present are considered.
CONFIG_ENV_PREFIXES = (
    "APP_",
    "DATABASE",
    "MIGRATION_",
    "WORKER_",
    "CC0_",
    "OBJECT_STORE_",
    "READINESS_",
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
    return {name.upper() for name in Settings.model_fields} | set(COMPOSE_ALLOWLIST)


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


@lru_cache
def get_settings() -> Settings:
    """Load at process entry only; requests use app.state.settings and resources."""
    return Settings()
