"""Failure classification. This is what decides retry versus dead-letter.

A failure here is a job outcome, never a rights outcome. An eligible record
whose bytes cannot be fetched keeps its CC0 decision; only the job dies.

`reason` is a short stable token, safe to store in jobs.last_error and to log.
Provider bodies and driver text stay out of it.
"""


class IngestFailure(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class TransientFailure(IngestFailure):
    """Worth another attempt: timeout, connection reset, 429, 5xx."""

    def __init__(self, reason: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(reason, detail)
        # Honored over computed backoff when a provider sends Retry-After.
        self.retry_after = retry_after


class PermanentFailure(IngestFailure):
    """Another attempt cannot help: 4xx, a refused host, or an unusable body.

    These go straight to the dead-letter state instead of burning the transient
    retry budget on an answer that will not change.
    """
