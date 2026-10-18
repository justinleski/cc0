"""Export job execution lifecycle; the packaging workflow lives in exports."""

import logging

from app.core.config import Settings
from app.db import catalog
from app.exports.package_service import FAILED, ExportOutcome, PackageService
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.integrations.object_store import ObjectStoreUnavailable
from app.worker import jobs as job_module
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore
from app.worker.lease import LeaseKeeper

logger = logging.getLogger("cc0.worker.export")
CLAIM_LOST = "claim_lost"


class ExportWorker:
    def __init__(self, *, settings: Settings, job_store: JobStore, service: PackageService):
        self._settings = settings
        self._jobs = job_store
        self.service = service

    def drain(self, max_jobs: int | None = None) -> list[ExportOutcome]:
        self._jobs.reap_exhausted(kind=catalog.JOB_KIND_EXPORT)
        self.service.sweep()
        outcomes: list[ExportOutcome] = []
        budget = max_jobs if max_jobs is not None else self._settings.ingest_batch_limit
        for _ in range(budget):
            job = self._jobs.claim(
                lease_seconds=self._settings.ingest_lease_seconds, kind=catalog.JOB_KIND_EXPORT
            )
            if job is None:
                break
            outcomes.append(self.run_claimed(job))
        return outcomes

    def run_claimed(self, job: ClaimedJob) -> ExportOutcome:
        export_id = str(job.payload.get("export_id", ""))
        try:
            with LeaseKeeper(
                self._jobs,
                job,
                lease_seconds=self._settings.ingest_lease_seconds,
                deadline_seconds=self._settings.ingest_job_deadline_seconds,
            ) as keeper:
                return self.service.execute(job, keeper.check)
        except ClaimLost:
            return ExportOutcome(export_id, CLAIM_LOST, job_settled=True)
        except PermanentFailure as failure:
            return self._dead(job, export_id, failure.reason)
        except TransientFailure as failure:
            return self._schedule_retry(job, export_id, failure)
        except ObjectStoreUnavailable as failure:
            return self._schedule_retry(
                job, export_id, TransientFailure("object_store", str(failure))
            )

    def _dead(self, job: ClaimedJob, export_id: str, reason: str) -> ExportOutcome:
        try:
            self.service.fail(job, export_id, reason)
        except ClaimLost:
            return ExportOutcome(export_id, CLAIM_LOST, reason, job_settled=True)
        logger.warning("export job dead export_id=%s reason=%s", export_id, reason)
        return ExportOutcome(export_id, FAILED, reason, job_settled=True)

    def _schedule_retry(
        self, job: ClaimedJob, export_id: str, failure: TransientFailure
    ) -> ExportOutcome:
        if job.attempts_exhausted:
            return self._dead(job, export_id, failure.reason)
        delay = failure.retry_after
        if delay is None:
            delay = job_module.backoff_seconds(
                job.attempt_count,
                base_seconds=self._settings.ingest_backoff_base_seconds,
                max_seconds=self._settings.ingest_backoff_max_seconds,
            )
        if not self._jobs.retry_later(job, failure.reason, delay):
            return ExportOutcome(export_id, CLAIM_LOST, failure.reason, job_settled=True)
        return ExportOutcome(export_id, catalog.JOB_RETRY_WAIT, failure.reason, job_settled=True)
