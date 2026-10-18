"""Run one export job: snapshot, recheck rights, package, upload, publish.

Ordering is what makes this restart-safe. The archive is built in a local temp
file and uploaded under a key that contains its own SHA-256, so a retry that
produces different bytes writes a different object instead of overwriting one a
reader may hold. The database row is the only thing that names the live archive,
and it is updated in one fenced transaction that rechecks rights. A crash at any
earlier point leaves at most an unreferenced object, which cleanup removes.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import Settings
from app.db import catalog
from app.exports.packager import build_archive
from app.exports.worker_repository import ExportWorkerRepository
from app.ingestion.errors import PermanentFailure
from app.integrations.object_store import ObjectStore
from app.worker.jobs import ClaimedJob

logger = logging.getLogger("cc0.worker.export")

PUBLISHED = "published"
ALREADY_DONE = "already_done"
FAILED = "failed"


@dataclass(frozen=True)
class ExportOutcome:
    export_id: str
    result: str
    detail: str = ""
    job_settled: bool = False


def artifact_key(owner_id: UUID, export_id: UUID, sha256: str) -> str:
    return f"{catalog.EXPORT_PREFIX}/{owner_id}/{export_id}/{sha256}.zip"


class PackageService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: ExportWorkerRepository,
        object_store: ObjectStore,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._objects = object_store

    def execute(self, job: ClaimedJob, checkpoint: Callable[[], None]) -> ExportOutcome:
        try:
            export_id = UUID(str(job.payload.get("export_id")))
        except ValueError as exc:
            raise PermanentFailure("export_payload_invalid", "") from exc
        export = self._repository.get(export_id)
        if export is None:
            raise PermanentFailure("export_missing", "")
        if export.status != catalog.EXPORT_QUEUED:
            self._repository.settle_already_done(job)
            return ExportOutcome(str(export_id), ALREADY_DONE, export.status, job_settled=True)

        items = self._repository.items(export_id)
        packaged = build_archive(
            export_id=str(export_id),
            board_id=None if export.board_id is None else str(export.board_id),
            board_name=export.board_name,
            exported_at=export.created_at,
            items=items,
            read_original=self._read_original,
            checkpoint=checkpoint,
        )
        if packaged.file is None:
            # Nothing eligible and readable is left. An empty archive is never published.
            self._repository.fail(job, export_id, "no_eligible_assets")
            return ExportOutcome(str(export_id), FAILED, "no_eligible_assets", job_settled=True)

        key = artifact_key(export.owner_user_id, export_id, packaged.sha256)
        # A failure past this point may leave an unpublished object. It is not
        # deleted here: the key is content-addressed, so a concurrent attempt that
        # produced identical bytes shares it. Cleanup removes unreferenced objects.
        try:
            checkpoint()
            self._objects.put_file(
                key, packaged.file, "application/zip", packaged.sha256, packaged.size
            )
            checkpoint()
            published = self._repository.publish(
                job,
                export_id,
                artifact_key=key,
                sha256=packaged.sha256,
                size=packaged.size,
                included=[UUID(asset) for asset in packaged.included],
                excluded=list(packaged.excluded),
                ttl_hours=self._settings.export_ttl_hours,
            )
        finally:
            packaged.file.close()
        logger.info(
            "export published export_id=%s included=%s excluded=%s bytes=%s",
            export_id,
            len(packaged.included),
            len(packaged.excluded),
            packaged.size,
        )
        return ExportOutcome(
            str(export_id), PUBLISHED if published else ALREADY_DONE, job_settled=True
        )

    def sweep(self) -> None:
        self._repository.sweep()

    def fail(self, job: ClaimedJob, export_id: str, reason: str) -> None:
        try:
            parsed: UUID | None = UUID(export_id)
        except ValueError:
            parsed = None
        self._repository.fail(job, parsed, reason)

    def _read_original(self, key: str) -> bytes | None:
        return self._objects.get_bytes(key)

    def cleanup(self, *, batch: int = 100) -> dict[str, int]:
        """Retire archives that expired or lost rights eligibility, then sweep orphans.

        The object is deleted before the row is retired, so a failed delete leaves
        the row READY and the next run tries again. Orphans are objects under the
        export prefix that no row references and that are older than the grace
        period, which covers an upload still waiting to be published.
        """
        self._repository.sweep()
        counts = {"expired": 0, "rights_withdrawn": 0, "orphans": 0}
        for due in self._repository.due_for_retirement(batch):
            if due.artifact_key is not None:
                self._objects.delete(due.artifact_key)
            if due.expired:
                self._repository.retire(due.export_id, status=catalog.EXPORT_EXPIRED, reason=None)
                counts["expired"] += 1
            else:
                self._repository.retire(
                    due.export_id, status=catalog.EXPORT_FAILED, reason="rights_changed"
                )
                counts["rights_withdrawn"] += 1
        cutoff = datetime.now(UTC) - timedelta(hours=self._settings.export_orphan_grace_hours)
        referenced = self._repository.referenced_keys()
        for key, modified in list(self._objects.list_objects(f"{catalog.EXPORT_PREFIX}/")):
            if key not in referenced and modified < cutoff:
                self._objects.delete(key)
                counts["orphans"] += 1
        return counts
