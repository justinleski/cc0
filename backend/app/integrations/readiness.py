import logging
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, TimeoutError

logger = logging.getLogger("cc0.api")

Check = Callable[[], None]


class DependencyUnavailable(Exception):
    """An operational failure; only its exception class is logged."""


def check_postgres(engine: Engine, timeout_seconds: float) -> None:
    # The engine is the shared API pool, whose checkout timeout is a request
    # budget rather than a probe budget, so the probe statement timeout comes
    # from the caller. SET LOCAL lasts for this transaction only, so a later
    # request on the same connection cannot inherit it.
    timeout_ms = max(int(timeout_seconds * 1000), 1)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            connection.execute(text("SELECT 1"))
    except (DBAPIError, TimeoutError) as exc:
        raise DependencyUnavailable(type(exc).__name__) from exc


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
