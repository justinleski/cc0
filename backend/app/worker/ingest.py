"""Ingestion job execution lifecycle; business workflow lives in ingestion."""

import logging

from app.core.config import Settings
from app.db import catalog
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.ingestion.models import IngestOutcome
from app.ingestion.service import IngestService
from app.integrations.object_store import ObjectStoreUnavailable
from app.worker import jobs as job_module
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore
from app.worker.lease import LeaseKeeper

logger = logging.getLogger("cc0.worker")
CLAIM_LOST = "claim_lost"


class IngestWorker:
    def __init__(self, *, settings: Settings, job_store: JobStore, service: IngestService):
        self._settings = settings
        self._jobs = job_store
        self.service = service

    # ---- worker loop ----

    def drain(self, max_jobs: int | None = None) -> list[IngestOutcome]:
        """Run claimed jobs one at a time.

        One job in flight means one in-flight source request per provider, which
        is the configured limit for a single local worker.
        """
        self._jobs.reap_exhausted()
        outcomes: list[IngestOutcome] = []
        budget = max_jobs if max_jobs is not None else self._settings.ingest_batch_limit
        for _ in range(budget):
            job = self._jobs.claim(lease_seconds=self._settings.ingest_lease_seconds)
            if job is None:
                break
            outcomes.append(self.run_claimed(job))
        return outcomes

    def run_claimed(self, job: ClaimedJob) -> IngestOutcome:
        external_id = str(job.payload.get("external_id", ""))
        try:
            with LeaseKeeper(
                self._jobs,
                job,
                lease_seconds=self._settings.ingest_lease_seconds,
                deadline_seconds=self._settings.ingest_job_deadline_seconds,
            ) as lease:
                outcome = self.service.execute(job, external_id, lease.check)
        except ClaimLost:
            return self._abandon(job, external_id, "claim_lost")
        except PermanentFailure as failure:
            # Another attempt cannot change this answer, so skip the retry
            # budget entirely and dead-letter the row for inspection.
            if not self._jobs.mark_dead(job, failure.reason):
                return self._abandon(job, external_id, failure.reason)
            logger.warning(
                "ingest job dead external_id=%s reason=%s attempt=%s",
                external_id,
                failure.reason,
                job.attempt_count,
            )
            return IngestOutcome(external_id, catalog.JOB_DEAD, failure.reason, job_settled=True)
        except TransientFailure as failure:
            return self._schedule_retry(job, external_id, failure)
        except ObjectStoreUnavailable as failure:
            return self._schedule_retry(
                job, external_id, TransientFailure("object_store", str(failure))
            )
        if not outcome.job_settled and not self._jobs.succeed(job):
            return self._abandon(job, external_id, outcome.result)
        return outcome

    def _abandon(self, job: ClaimedJob, external_id: str, detail: str) -> IngestOutcome:
        """Walk away without writing. The row belongs to its current claim.

        The work this attempt did is not lost: objects are content-addressed
        and the catalog is what a later attempt reads, so whoever holds the job
        now reaches the same place.
        """
        logger.warning(
            "ingest job claim lost external_id=%s attempt=%s detail=%s",
            external_id,
            job.attempt_count,
            detail,
        )
        return IngestOutcome(external_id, CLAIM_LOST, detail, job_settled=True)

    def _schedule_retry(
        self,
        job: ClaimedJob,
        external_id: str,
        failure: TransientFailure,
    ) -> IngestOutcome:
        if job.attempts_exhausted:
            if not self._jobs.mark_dead(job, failure.reason):
                return self._abandon(job, external_id, failure.reason)
            if failure.reason == "rate_limited":
                self._defer_provider(
                    job, failure.retry_after or self._settings.ingest_backoff_base_seconds
                )
            logger.warning(
                "ingest job dead after retries external_id=%s reason=%s attempts=%s",
                external_id,
                failure.reason,
                job.attempt_count,
            )
            return IngestOutcome(external_id, catalog.JOB_DEAD, failure.reason, job_settled=True)
        delay = failure.retry_after
        if delay is None or (failure.reason == "rate_limited" and delay <= 0):
            delay = job_module.backoff_seconds(
                job.attempt_count,
                base_seconds=self._settings.ingest_backoff_base_seconds,
                max_seconds=self._settings.ingest_backoff_max_seconds,
            )
        if not self._jobs.retry_later(job, failure.reason, delay):
            return self._abandon(job, external_id, failure.reason)
        if failure.reason == "rate_limited":
            self._defer_provider(job, delay)
        logger.info(
            "ingest job retrying external_id=%s reason=%s attempt=%s delay=%.1f",
            external_id,
            failure.reason,
            job.attempt_count,
            delay,
        )
        return IngestOutcome(external_id, catalog.JOB_RETRY_WAIT, failure.reason, job_settled=True)

    def _defer_provider(self, job: ClaimedJob, delay: float) -> None:
        provider = job.payload.get("provider")
        if isinstance(provider, str) and provider:
            self._jobs.defer_provider(provider, delay)
