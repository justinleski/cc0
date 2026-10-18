"""Export persistence for the worker role, fenced by the job claim."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.exports import worker_queries as queries
from app.ingestion.errors import TransientFailure
from app.worker.jobs import ClaimedJob, hold_claim

RIGHTS_CHANGED_DURING_PACKAGING = "rights_changed_during_packaging"


@dataclass(frozen=True)
class ExportRow:
    id: UUID
    owner_user_id: UUID
    board_id: UUID | None
    board_name: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class Retirement:
    export_id: UUID
    artifact_key: str | None
    expired: bool


class ExportWorkerRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, export_id: UUID) -> ExportRow | None:
        with self._engine.begin() as connection:
            row = connection.execute(text(queries.EXPORT), {"export_id": export_id}).one_or_none()
        return None if row is None else ExportRow(**row._mapping)

    def items(self, export_id: UUID) -> list[dict[str, Any]]:
        with self._engine.begin() as connection:
            rows = connection.execute(text(queries.ITEMS), {"export_id": export_id}).all()
        return [dict(row._mapping) for row in rows]

    def settle_already_done(self, job: ClaimedJob) -> None:
        """The export already left QUEUED (another attempt finished it). Close the job."""
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            connection.execute(text(queries.SUCCEED_JOB), {"job_id": job.id})

    def publish(
        self,
        job: ClaimedJob,
        export_id: UUID,
        *,
        artifact_key: str,
        sha256: str,
        size: int,
        included: list[UUID],
        excluded: list[tuple[str, str]],
        ttl_hours: int,
    ) -> bool:
        """Make the archive downloadable, in one transaction under the job fence.

        Eligibility is read again here, after locking the asset rows, because
        packaging can take long enough for a rights decision to land. If any
        packaged asset is no longer eligible nothing is published and the job
        retries, which rebuilds the archive without it. Returns False when the
        export was already finished by an earlier attempt.
        """
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            current = connection.execute(
                text(queries.LOCK_EXPORT), {"export_id": export_id}
            ).one()
            if current.status != "QUEUED":
                connection.execute(text(queries.SUCCEED_JOB), {"job_id": job.id})
                return False
            connection.execute(text(queries.LOCK_ASSETS), {"asset_ids": included})
            eligible = {
                row.id
                for row in connection.execute(
                    text(queries.STILL_ELIGIBLE), {"asset_ids": included}
                )
            }
            if eligible != set(included):
                raise TransientFailure(RIGHTS_CHANGED_DURING_PACKAGING)
            params = {"export_id": export_id}
            connection.execute(text(queries.MARK_PACKAGED), {**params, "asset_ids": included})
            for asset_id, reason in excluded:
                connection.execute(
                    text(queries.MARK_EXCLUDED),
                    {**params, "asset_id": UUID(asset_id), "reason": reason},
                )
            connection.execute(
                text(queries.PUBLISH),
                {
                    **params,
                    "artifact_key": artifact_key,
                    "sha256": sha256,
                    "size": size,
                    "included": len(included),
                    "excluded": len(excluded),
                    "ttl_hours": ttl_hours,
                },
            )
            connection.execute(text(queries.SUCCEED_JOB), {"job_id": job.id})
        return True

    def fail(self, job: ClaimedJob, export_id: UUID | None, reason: str) -> None:
        """Permanent outcome: the export and its job both end, atomically."""
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            if export_id is not None:
                connection.execute(text(queries.FAIL), {"export_id": export_id, "reason": reason})
            connection.execute(text(queries.DEAD_JOB), {"job_id": job.id, "reason": reason})

    def sweep(self) -> None:
        """Reconcile export rows with jobs that were dead-lettered or redriven."""
        with self._engine.begin() as connection:
            connection.execute(text(queries.FAIL_DEAD_EXPORTS))
            connection.execute(text(queries.REQUEUE_REDRIVEN))

    def due_for_retirement(self, limit: int) -> list[Retirement]:
        with self._engine.begin() as connection:
            rows = connection.execute(text(queries.DUE_FOR_RETIREMENT), {"limit": limit})
            return [Retirement(row.id, row.artifact_key, row.expired) for row in rows]

    def retire(self, export_id: UUID, *, status: str, reason: str | None) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(queries.RETIRE),
                {"export_id": export_id, "status": status, "reason": reason},
            )

    def referenced_keys(self) -> set[str]:
        with self._engine.begin() as connection:
            return {row.artifact_key for row in connection.execute(text(queries.REFERENCED_KEYS))}
