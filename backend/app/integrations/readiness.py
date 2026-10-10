import logging
from collections.abc import Callable

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings

logger = logging.getLogger("cc0.api")

Check = Callable[[], None]


class DependencyUnavailable(Exception):
    """A readiness dependency rejected the check. The message is for logs, not clients."""


def check_postgres(settings: Settings) -> None:
    timeout = int(settings.readiness_timeout_seconds)
    try:
        with psycopg.connect(
            settings.database_url,
            connect_timeout=max(timeout, 1),
        ) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        raise DependencyUnavailable(type(exc).__name__) from exc


def check_object_store(settings: Settings) -> None:
    timeout = settings.readiness_timeout_seconds
    client = boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        aws_access_key_id=settings.object_store_access_key,
        aws_secret_access_key=settings.object_store_secret_key,
        region_name=settings.object_store_region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"max_attempts": 1},
        ),
    )
    try:
        client.head_bucket(Bucket=settings.object_store_bucket)
    except (BotoCoreError, ClientError) as exc:
        raise DependencyUnavailable(type(exc).__name__) from exc


def run_checks(checks: dict[str, Check]) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
        except DependencyUnavailable as exc:
            detail = str(exc)
            if not detail.isidentifier():
                detail = "DependencyUnavailable"
            logger.warning("readiness check failed check=%s error_type=%s", name, detail)
            results[name] = "error"
        except Exception as exc:
            logger.warning(
                "readiness check failed check=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            results[name] = "error"
        else:
            results[name] = "ok"
    return results
