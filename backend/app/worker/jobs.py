"""The job row: the single-flight lock, the retry ledger, and the dead letter.

PostgreSQL is the only job store. A later SQS or ElasticMQ queue would be a
wake-up mechanism, not a second source of truth, so "dead letter" here is a
terminal `dead` state on the same row an operator already has to look at. That
keeps the failure joined to the source record, the rights evidence, and the
attempt history instead of living in a topic that can disagree with them.

`kind` names an independently retryable unit of work. M1 has exactly one,
`ingest`; M2 adds `embed` with its own dedupe key and attempt budget. There is
no stage column: a retry reads progress from the catalog and the object store,
because a committed stage marker can disagree with what was actually written.
"""

import json
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.db import catalog

# States a repeated ingest request may reset. `leased` is in flight, and `dead`
# waits for an explicit operator redrive.
RESETTABLE_STATES = (catalog.JOB_PENDING, catalog.JOB_RETRY_WAIT, catalog.JOB_SUCCEEDED)


def dedupe_key(provider: str, external_id: str) -> str:
    return f"{catalog.JOB_KIND_INGEST}:{provider}:{external_id}"


def backoff_seconds(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with full jitter, in [half, full] of the window.

    Jitter keeps a batch of records that failed together from retrying in
    lockstep. The floor at half the window means a retry always waits.
    """
    window = min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds)
    generator = rng or random
    return window * (0.5 + 0.5 * generator.random())


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    dedupe_key: str
    attempt_count: int
    max_attempts: int
    payload: dict[str, Any]

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempt_count >= self.max_attempts


@dataclass(frozen=True)
class JobRow:
    dedupe_key: str
    state: str
    attempt_count: int
    last_error: str | None
    payload: dict[str, Any]


class JobStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue(
        self,
        dedupe: str,
        payload: dict[str, Any],
        *,
        max_attempts: int,
        kind: str = catalog.JOB_KIND_INGEST,
    ) -> bool:
        """Create the job row for this identity. True when it was created.

        A second ingestion of the same record finds the existing row instead of
        queueing a duplicate, which is what keeps a repeat run from downloading
        the same bytes twice.
        """
        with self._engine.begin() as connection:
            created = connection.execute(
                text(
                    """
                    INSERT INTO jobs (kind, dedupe_key, state, payload, max_attempts)
                    VALUES (:kind, :dedupe_key, :state, CAST(:payload AS jsonb), :max_attempts)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "kind": kind,
                    "dedupe_key": dedupe,
                    "state": catalog.JOB_PENDING,
                    "payload": _json(payload),
                    "max_attempts": max_attempts,
                },
            ).scalar_one_or_none()
        return created is not None

    def request_run(self, dedupe: str) -> bool:
        """Make an existing job runnable again and reset its attempt state.

        Used when a negative-cache record is due for recheck. A `leased` row is
        left alone because another worker holds it, and a `dead` row is left
        alone because a failure an operator has not seen must not be cleared by
        someone re-running ingestion.
        """
        with self._engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :pending,
                        attempt_count = 0,
                        next_run_at = NULL,
                        leased_until = NULL,
                        updated_at = now()
                    WHERE dedupe_key = :dedupe_key AND state = ANY(:resettable)
                    RETURNING id
                    """
                ),
                {
                    "dedupe_key": dedupe,
                    "pending": catalog.JOB_PENDING,
                    "resettable": list(RESETTABLE_STATES),
                },
            ).scalar_one_or_none()
        return updated is not None

    def claim(
        self,
        *,
        lease_seconds: int,
        kind: str = catalog.JOB_KIND_INGEST,
    ) -> ClaimedJob | None:
        """Take one runnable job. FOR UPDATE SKIP LOCKED keeps it single-flight.

        The attempt counter increments here rather than on failure, so a worker
        that crashes mid-job still spends an attempt when its lease expires. A
        job that poisons the process therefore cannot be reclaimed forever.
        """
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT id FROM jobs
                        WHERE kind = :kind
                          AND attempt_count < max_attempts
                          AND (
                              state = :pending
                              OR (state = :retry_wait
                                  AND (next_run_at IS NULL OR next_run_at <= now()))
                              OR (state = :leased AND leased_until < now())
                          )
                        ORDER BY COALESCE(next_run_at, created_at)
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs SET
                        state = :leased,
                        leased_until = now() + make_interval(secs => :lease_seconds),
                        attempt_count = attempt_count + 1,
                        updated_at = now()
                    WHERE id = (SELECT id FROM candidate)
                    RETURNING id, kind, dedupe_key, attempt_count, max_attempts, payload
                    """
                ),
                {
                    "kind": kind,
                    "pending": catalog.JOB_PENDING,
                    "retry_wait": catalog.JOB_RETRY_WAIT,
                    "leased": catalog.JOB_LEASED,
                    "lease_seconds": lease_seconds,
                },
            ).one_or_none()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            kind=row.kind,
            dedupe_key=row.dedupe_key,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            payload=row.payload or {},
        )

    def reap_exhausted(self, *, kind: str = catalog.JOB_KIND_INGEST) -> int:
        """Dead-letter jobs that can no longer be claimed.

        A lease that expired on the last attempt would otherwise sit in `leased`
        forever, invisible to both the claim query and the operator.
        """
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :dead,
                        dead_at = now(),
                        leased_until = NULL,
                        last_error = COALESCE(last_error, :reason),
                        updated_at = now()
                    WHERE kind = :kind
                      AND attempt_count >= max_attempts
                      AND state IN (:leased, :retry_wait)
                      AND (leased_until IS NULL OR leased_until < now())
                    """
                ),
                {
                    "kind": kind,
                    "dead": catalog.JOB_DEAD,
                    "leased": catalog.JOB_LEASED,
                    "retry_wait": catalog.JOB_RETRY_WAIT,
                    "reason": "attempts_exhausted",
                },
            )
        return result.rowcount

    def succeed(self, job_id: UUID) -> None:
        self._set_state(
            job_id,
            """
            state = :state, leased_until = NULL, next_run_at = NULL,
            last_error = NULL, updated_at = now()
            """,
            {"state": catalog.JOB_SUCCEEDED},
        )

    def retry_later(self, job_id: UUID, reason: str, delay_seconds: float) -> None:
        self._set_state(
            job_id,
            """
            state = :state, leased_until = NULL,
            next_run_at = now() + make_interval(secs => :delay),
            last_error = :reason, updated_at = now()
            """,
            {"state": catalog.JOB_RETRY_WAIT, "delay": delay_seconds, "reason": reason},
        )

    def mark_dead(self, job_id: UUID, reason: str) -> None:
        """Terminal. Nothing retries this row until an operator redrives it."""
        self._set_state(
            job_id,
            """
            state = :state, dead_at = now(), leased_until = NULL,
            next_run_at = NULL, last_error = :reason, updated_at = now()
            """,
            {"state": catalog.JOB_DEAD, "reason": reason},
        )

    def merge_payload(self, job_id: UUID, patch: dict[str, Any]) -> None:
        """Record a hint for the next attempt. Hints are verified, not trusted."""
        self._set_state(
            job_id,
            "payload = payload || CAST(:patch AS jsonb), updated_at = now()",
            {"patch": _json(patch)},
        )

    def redrive(self, dedupe: str) -> bool:
        """Operator action: return a dead job to pending and reset its attempts.

        `last_error` survives on purpose, so why it died is still readable after
        the retry succeeds.
        """
        with self._engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :pending,
                        attempt_count = 0,
                        dead_at = NULL,
                        next_run_at = NULL,
                        leased_until = NULL,
                        updated_at = now()
                    WHERE dedupe_key = :dedupe_key AND state = :dead
                    RETURNING id
                    """
                ),
                {
                    "dedupe_key": dedupe,
                    "pending": catalog.JOB_PENDING,
                    "dead": catalog.JOB_DEAD,
                },
            ).scalar_one_or_none()
        return updated is not None

    def get(self, dedupe: str) -> JobRow | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT dedupe_key, state, attempt_count, last_error, payload
                    FROM jobs WHERE dedupe_key = :dedupe_key
                    """
                ),
                {"dedupe_key": dedupe},
            ).one_or_none()
        return None if row is None else _job_row(row)

    def list_by_state(self, state: str, limit: int = 50) -> list[JobRow]:
        with self._engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT dedupe_key, state, attempt_count, last_error, payload
                    FROM jobs WHERE state = :state
                    ORDER BY updated_at DESC LIMIT :limit
                    """
                ),
                {"state": state, "limit": limit},
            ).all()
        return [_job_row(row) for row in rows]

    def _set_state(self, job_id: UUID, assignments: str, params: dict[str, Any]) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(f"UPDATE jobs SET {assignments} WHERE id = :id"),
                {"id": job_id, **params},
            )


def _job_row(row: Any) -> JobRow:
    return JobRow(
        dedupe_key=row.dedupe_key,
        state=row.state,
        attempt_count=row.attempt_count,
        last_error=row.last_error,
        payload=row.payload or {},
    )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value)
