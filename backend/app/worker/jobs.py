"""The job row: the single-flight lock, the retry ledger, and the dead letter.

PostgreSQL is the only job store. A later SQS or ElasticMQ queue would be a
wake-up mechanism, not a second source of truth, so "dead letter" here is a
terminal `dead` state on the same row an operator already has to look at. That
keeps the failure joined to the source record, the rights evidence, and the
attempt history instead of living in a topic that can disagree with them.

`kind` names an independently retryable unit of work. `ingest` downloads and
stores. `embed` encodes an original that is already in object storage, with its
own dedupe key and attempt budget, so a model failure does not re-lease the
download. There is no stage column: a retry reads progress from the catalog and
the object store, because a committed stage marker can disagree with what was
actually written.

Claiming issues a `claim_token`, and every write a worker makes afterwards has
to present it while the lease is still live. Without that fence a worker whose
lease expired mid-job could overwrite the result of the worker that reclaimed
and finished it, which no amount of attempt counting would catch.
"""

import json
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.db import catalog

# The only state a repeated ingest request may reset, and only for a due
# negative-cache recheck. `leased` is in flight, `dead` waits for an explicit
# operator redrive, and `pending`/`retry_wait` are already going to run: giving
# those back a full attempt budget would let repeated ingest runs erase
# backoff, Retry-After, and the attempt cap that makes a job die.
RESETTABLE_STATES = (catalog.JOB_SUCCEEDED,)

# A job the claim query can no longer see: out of attempts, and either parked
# in retry_wait or holding a lease that has run out.
_UNCLAIMABLE = """
    kind = :kind
    AND attempt_count >= max_attempts
    AND state IN (:leased, :retry_wait)
    AND (leased_until IS NULL OR leased_until < now())
"""

# The asset this job exists to create, reached through the same identity the
# dedupe key is built from rather than by parsing the payload.
_HAS_STORED_ASSET = """
    EXISTS (
        SELECT 1 FROM assets
        JOIN source_records ON source_records.id = assets.source_record_id
        JOIN providers ON providers.id = source_records.provider_id
        WHERE assets.status = ANY(:present)
          AND jobs.dedupe_key = :prefix || providers.name || ':' || source_records.external_id
    )
"""

# The condition every worker write carries. Shared with the catalog store, so
# an asset or a rights decision is committed under the same fence as the job
# state it belongs to.
CLAIM_HELD = """
    id = :job_id
    AND claim_token = :claim_token
    AND state = :leased
    AND leased_until > now()
"""


def dedupe_key(provider: str, external_id: str) -> str:
    return f"{catalog.JOB_KIND_INGEST}:{provider}:{external_id}"


def embed_dedupe_key(model_key: str, model_version: str, asset_id: UUID) -> str:
    """One embedding job per asset and checkpoint. A new version is a new row."""
    return f"{catalog.JOB_KIND_EMBED}:{model_key}:{model_version}:{asset_id}"


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


class ClaimLost(Exception):
    """This worker no longer holds the job it is working on.

    Raised instead of writing, because the row now belongs to whoever reclaimed
    the expired lease. The work already committed elsewhere stands, and this
    attempt must not record a success, a retry, or a dead letter over it.
    """

    def __init__(self, job_id: UUID) -> None:
        super().__init__(f"claim lost for job {job_id}")
        self.job_id = job_id


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    dedupe_key: str
    attempt_count: int
    max_attempts: int
    payload: dict[str, Any]
    # Proof that this process is the one holding the lease. Every later write
    # carries it, so a reclaimed job ignores its previous worker.
    claim_token: UUID

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


def hold_claim(connection: Connection, job: ClaimedJob) -> None:
    """Lock the job row for this transaction, or refuse to write at all.

    `FOR UPDATE` keeps the row from being reclaimed between this check and the
    commit, so the catalog write and the job state it belongs to settle on the
    same answer about who owns the work.
    """
    held = connection.execute(
        text(f"SELECT id FROM jobs WHERE {CLAIM_HELD} FOR UPDATE"),
        {
            "job_id": job.id,
            "claim_token": job.claim_token,
            "leased": catalog.JOB_LEASED,
        },
    ).scalar_one_or_none()
    if held is None:
        raise ClaimLost(job.id)


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

    def request_recheck_run(self, dedupe: str, source_record_id: UUID) -> bool:
        """Re-run a finished job because its negative cache is due again.

        The recheck condition is part of this statement rather than a prior
        read, so a repeat ingestion cannot reset a job on a deadline that
        passed between the two. Everything else is left exactly as it is: a
        `retry_wait` row keeps the backoff and the attempts it has already
        spent, a `leased` row belongs to the worker holding it, and a `dead`
        row waits for an operator who has seen the failure.
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
                        claim_token = NULL,
                        updated_at = now()
                    WHERE dedupe_key = :dedupe_key
                      AND state = ANY(:resettable)
                      AND EXISTS (
                          SELECT 1 FROM source_records
                          WHERE source_records.id = :source_record_id
                            AND source_records.eligibility_status = ANY(:cached)
                            AND source_records.next_recheck_at IS NOT NULL
                            AND source_records.next_recheck_at <= now()
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM assets
                          WHERE assets.source_record_id = :source_record_id
                            AND assets.status = ANY(:present)
                      )
                    RETURNING id
                    """
                ),
                {
                    "dedupe_key": dedupe,
                    "source_record_id": source_record_id,
                    "pending": catalog.JOB_PENDING,
                    "resettable": list(RESETTABLE_STATES),
                    "cached": list(catalog.CACHED_STATUSES),
                    "present": list(catalog.ASSET_HAS_BYTES),
                },
            ).scalar_one_or_none()
        return updated is not None

    def claim(
        self,
        *,
        lease_seconds: int,
        kind: str = catalog.JOB_KIND_INGEST,
        model_key: str | None = None,
        model_version: str | None = None,
    ) -> ClaimedJob | None:
        """Take one runnable job. FOR UPDATE SKIP LOCKED keeps it single-flight.

        The attempt counter increments here rather than on failure, so a worker
        that crashes mid-job still spends an attempt when its lease expires. A
        job that poisons the process therefore cannot be reclaimed forever.

        A new token is issued on every claim, including a reclaim, so the
        previous holder of this row can no longer write to it.

        An embed claim names the loaded provider identity and locks only those
        rows. A job for another checkpoint stays pending, retry_wait, or leased
        by its owner, and this claim does not spend its attempts.
        """
        params: dict[str, Any] = {
            "kind": kind,
            "pending": catalog.JOB_PENDING,
            "retry_wait": catalog.JOB_RETRY_WAIT,
            "leased": catalog.JOB_LEASED,
            "lease_seconds": lease_seconds,
        }
        model_sql = ""
        if kind == catalog.JOB_KIND_EMBED:
            if not model_key or not model_version:
                raise ValueError("an embed claim names the loaded checkpoint")
            model_sql = """
                          AND payload->>'model_key' = :model_key
                          AND payload->>'model_version' = :model_version
            """
            params["model_key"] = model_key
            params["model_version"] = model_version
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    f"""
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
                          {model_sql}
                        ORDER BY COALESCE(next_run_at, created_at)
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE jobs SET
                        state = :leased,
                        leased_until = now() + make_interval(secs => :lease_seconds),
                        claim_token = gen_random_uuid(),
                        attempt_count = attempt_count + 1,
                        updated_at = now()
                    WHERE id = (SELECT id FROM candidate)
                    RETURNING id, kind, dedupe_key, attempt_count, max_attempts,
                              payload, claim_token
                    """
                ),
                params,
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
            claim_token=row.claim_token,
        )

    def reap_exhausted(self, *, kind: str = catalog.JOB_KIND_INGEST) -> int:
        """Settle jobs that can no longer be claimed. Returns the dead count.

        A lease that expired on the last attempt would otherwise sit in `leased`
        forever, invisible to both the claim query and the operator.

        Durable catalog state is read first. An ingest job whose asset already
        has bytes, or an embed job whose vector row exists, finished its work,
        so it is reconciled as succeeded rather than dead-lettered.
        """
        if kind == catalog.JOB_KIND_EMBED:
            return self._reap_embed()
        with self._engine.begin() as connection:
            settled = {
                "kind": kind,
                "leased": catalog.JOB_LEASED,
                "retry_wait": catalog.JOB_RETRY_WAIT,
                "present": list(catalog.ASSET_HAS_BYTES),
                "prefix": f"{kind}:",
            }
            connection.execute(
                text(
                    f"""
                    UPDATE jobs SET
                        state = :succeeded,
                        leased_until = NULL,
                        claim_token = NULL,
                        next_run_at = NULL,
                        updated_at = now()
                    WHERE {_UNCLAIMABLE} AND {_HAS_STORED_ASSET}
                    """
                ),
                {**settled, "succeeded": catalog.JOB_SUCCEEDED},
            )
            result = connection.execute(
                text(
                    f"""
                    UPDATE jobs SET
                        state = :dead,
                        dead_at = now(),
                        leased_until = NULL,
                        claim_token = NULL,
                        last_error = COALESCE(last_error, :reason),
                        updated_at = now()
                    WHERE {_UNCLAIMABLE}
                    """
                ),
                {**settled, "dead": catalog.JOB_DEAD, "reason": "attempts_exhausted"},
            )
        return result.rowcount

    def _reap_embed(self) -> int:
        """An embed job is done when its vector row exists, whatever the asset status."""
        has_vector = f"""
            EXISTS (
                SELECT 1 FROM {catalog.EMBEDDINGS_TABLE}
                WHERE asset_id = (jobs.payload->>'asset_id')::uuid
                  AND model_key = jobs.payload->>'model_key'
                  AND model_version = jobs.payload->>'model_version'
            )
        """
        with self._engine.begin() as connection:
            settled = {
                "kind": catalog.JOB_KIND_EMBED,
                "leased": catalog.JOB_LEASED,
                "retry_wait": catalog.JOB_RETRY_WAIT,
            }
            connection.execute(
                text(
                    f"""
                    UPDATE jobs SET
                        state = :succeeded,
                        leased_until = NULL,
                        claim_token = NULL,
                        next_run_at = NULL,
                        updated_at = now()
                    WHERE {_UNCLAIMABLE} AND {has_vector}
                    """
                ),
                {**settled, "succeeded": catalog.JOB_SUCCEEDED},
            )
            result = connection.execute(
                text(
                    f"""
                    UPDATE jobs SET
                        state = :dead,
                        dead_at = now(),
                        leased_until = NULL,
                        claim_token = NULL,
                        last_error = COALESCE(last_error, :reason),
                        updated_at = now()
                    WHERE {_UNCLAIMABLE}
                    """
                ),
                {**settled, "dead": catalog.JOB_DEAD, "reason": "attempts_exhausted"},
            )
        return result.rowcount

    def succeed(self, job: ClaimedJob) -> bool:
        return self._fenced(
            job,
            """
            state = :state, leased_until = NULL, claim_token = NULL,
            next_run_at = NULL, last_error = NULL, updated_at = now()
            """,
            {"state": catalog.JOB_SUCCEEDED},
        )

    def retry_later(self, job: ClaimedJob, reason: str, delay_seconds: float) -> bool:
        return self._fenced(
            job,
            """
            state = :state, leased_until = NULL, claim_token = NULL,
            next_run_at = now() + make_interval(secs => :delay),
            last_error = :reason, updated_at = now()
            """,
            {"state": catalog.JOB_RETRY_WAIT, "delay": delay_seconds, "reason": reason},
        )

    def defer_provider(self, provider: str, delay_seconds: float) -> int:
        """Pause unclaimed ingest work after a provider-wide 429.

        Keep later retry deadlines intact; a rate limit must never shorten an
        existing backoff. Leased work belongs to its claim holder.
        """
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :retry_wait,
                        next_run_at = GREATEST(
                            COALESCE(next_run_at, now()),
                            now() + make_interval(secs => :delay)
                        ),
                        updated_at = now()
                    WHERE kind = :kind
                      AND payload->>'provider' = :provider
                      AND state IN (:pending, :retry_wait)
                    """
                ),
                {
                    "kind": catalog.JOB_KIND_INGEST,
                    "provider": provider,
                    "delay": delay_seconds,
                    "pending": catalog.JOB_PENDING,
                    "retry_wait": catalog.JOB_RETRY_WAIT,
                },
            )
        return result.rowcount

    def next_ingest_run_delay(self) -> float | None:
        """Seconds until ingest work is due or a live lease can be reclaimed."""
        with self._engine.begin() as connection:
            delay = connection.execute(
                text(
                    """
                    SELECT EXTRACT(EPOCH FROM MIN(
                        CASE
                            WHEN state = :pending THEN now()
                            WHEN state = :retry_wait THEN COALESCE(next_run_at, now())
                            ELSE COALESCE(leased_until, now())
                        END
                    ) - now())
                    FROM jobs
                    WHERE kind = :kind
                      AND state IN (:pending, :retry_wait, :leased)
                    """
                ),
                {
                    "kind": catalog.JOB_KIND_INGEST,
                    "pending": catalog.JOB_PENDING,
                    "retry_wait": catalog.JOB_RETRY_WAIT,
                    "leased": catalog.JOB_LEASED,
                },
            ).scalar_one()
        return None if delay is None else max(float(delay), 0.0)

    def mark_dead(self, job: ClaimedJob, reason: str) -> bool:
        """Terminal. Nothing retries this row until an operator redrives it."""
        return self._fenced(
            job,
            """
            state = :state, dead_at = now(), leased_until = NULL,
            claim_token = NULL, next_run_at = NULL, last_error = :reason,
            updated_at = now()
            """,
            {"state": catalog.JOB_DEAD, "reason": reason},
        )

    def merge_payload(self, job: ClaimedJob, patch: dict[str, Any]) -> bool:
        """Record a hint for the next attempt. Hints are verified, not trusted."""
        return self._fenced(
            job,
            "payload = payload || CAST(:patch AS jsonb), updated_at = now()",
            {"patch": _json(patch)},
        )

    def renew_lease(self, job: ClaimedJob, lease_seconds: int) -> bool:
        """Push the deadline out while this worker is still holding the claim.

        Renewal cannot revive a lost claim: the fence below already requires an
        unexpired lease, so a token that was reclaimed or allowed to expire
        stays dead.
        """
        return self._fenced(
            job,
            "leased_until = now() + make_interval(secs => :lease_seconds), updated_at = now()",
            {"lease_seconds": lease_seconds},
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
                        claim_token = NULL,
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

    def _fenced(self, job: ClaimedJob, assignments: str, params: dict[str, Any]) -> bool:
        """Write only while this claim is still the live lease on the row.

        False means the lease expired and someone else took the job. The caller
        decides what to do with that; what it must not do is write anyway.
        """
        with self._engine.begin() as connection:
            updated = connection.execute(
                text(f"UPDATE jobs SET {assignments} WHERE {CLAIM_HELD} RETURNING id"),
                {
                    "job_id": job.id,
                    "claim_token": job.claim_token,
                    "leased": catalog.JOB_LEASED,
                    **params,
                },
            ).scalar_one_or_none()
        return updated is not None


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
