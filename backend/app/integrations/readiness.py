import logging
from collections.abc import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, TimeoutError

from app.core.config import Settings

logger = logging.getLogger("cc0.api")

Check = Callable[[], None]


class DependencyUnavailable(Exception):
    """An operational failure; only its exception class is logged."""


def create_db_engine(settings: Settings) -> Engine:
    url = make_url(settings.database_url).set(drivername="postgresql+psycopg")
    timeout = settings.readiness_timeout_seconds
    # Readiness probes only. Pool size 2 with no overflow matches that job.
    # Do not reuse this engine for request work until the pool and the probe
    # statement timeout are split. connect_timeout stays here: psycopg counts
    # it in whole seconds, so a sub-second budget rounds up to 1. pool_timeout
    # stays on the readiness budget and may be fractional.
    return create_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=timeout,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(int(timeout), 1)},
    )


def check_postgres(engine: Engine) -> None:
    # SET LOCAL lasts for this transaction only, so a later request cannot
    # inherit the probe statement timeout. The value tracks pool_timeout, which
    # create_db_engine sets to the readiness budget.
    timeout_ms = max(int(engine.pool.timeout() * 1000), 1)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            connection.execute(text("SELECT 1"))
    except (DBAPIError, TimeoutError) as exc:
        raise DependencyUnavailable(type(exc).__name__) from exc


def create_s3_client(settings: Settings):
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
    return client


def check_object_store(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except (BotoCoreError, ClientError) as exc:
        raise DependencyUnavailable(type(exc).__name__) from exc


def _log_readiness_failure(request_id: str, check: str, error_type: str) -> None:
    # Class name only. Never str(exc), the driver text, or exc_info: those can
    # carry a URL or a password.
    logger.warning(
        "readiness check failed request_id=%s check=%s error_type=%s",
        request_id,
        check,
        error_type,
    )


def run_checks(checks: dict[str, Check], request_id: str) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
        except DependencyUnavailable as exc:
            # Operational failures keep going so the 503 reports every check.
            # Log the cause class (OperationalError), not DependencyUnavailable.
            _log_readiness_failure(request_id, name, type(exc.__cause__ or exc).__name__)
            results[name] = "error"
        except Exception as exc:
            error_type = type(exc).__name__
            _log_readiness_failure(request_id, name, error_type)
            # The middleware logs traceback and message. Raise a new error whose
            # message is only the class name so that traceback cannot contain
            # driver text. from None drops the chained cause.
            raise RuntimeError(error_type) from None
        else:
            results[name] = "ok"
    return results
