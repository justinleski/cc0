from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Process configuration. Secrets stay in the environment and are not logged."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    database_url: str = Field(
        default="postgresql://cc0_api:cc0_api_local_password@127.0.0.1:5433/cc0",
    )
    object_store_endpoint: str = "http://127.0.0.1:9000"
    object_store_bucket: str = "cc0"
    object_store_access_key: str = "cc0rustfs"
    object_store_secret_key: str = "cc0rustfs_local_password"
    object_store_region: str = "us-east-1"
    readiness_timeout_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
