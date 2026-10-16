"""Embedding job execution lifecycle; business workflow lives in embeddings."""

import logging

from app.core.config import Settings
from app.db import catalog
from app.embeddings.models import EmbedOutcome
from app.embeddings.service import EmbedService
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.integrations.object_store import ObjectStoreUnavailable
from app.worker import jobs as job_module
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore
from app.worker.lease import LeaseKeeper

logger = logging.getLogger("cc0.worker.embed")
CLAIM_LOST = "claim_lost"


class EmbedWorker:
    def __init__(self, *, settings: Settings, job_store: JobStore, service: EmbedService):
        self._settings = settings
        self._jobs = job_store
        self.service = service

    def drain(self, max_jobs: int | None = None) -> list[EmbedOutcome]:
        self.service.prepare()
        self._jobs.reap_exhausted(kind=catalog.JOB_KIND_EMBED)
        outcomes: list[EmbedOutcome] = []
        budget = max_jobs if max_jobs is not None else self._settings.ingest_batch_limit
        for _ in range(budget):
            job = self._jobs.claim(
                lease_seconds=self._settings.ingest_lease_seconds,
                kind=catalog.JOB_KIND_EMBED,
                model_key=self.service.model_key,
                model_version=self.service.model_version,
            )
            if job is None:
                break
            outcomes.append(self.run_claimed(job))
        return outcomes

    def run_claimed(self, job: ClaimedJob) -> EmbedOutcome:
        asset_id = str(job.payload.get("asset_id", ""))
        try:
            with LeaseKeeper(
                self._jobs,
                job,
                lease_seconds=self._settings.ingest_lease_seconds,
                deadline_seconds=self._settings.ingest_job_deadline_seconds,
            ):
                outcome = self.service.execute(job)
        except ClaimLost:
            return EmbedOutcome(asset_id, CLAIM_LOST, job_settled=True)
        except PermanentFailure as failure:
            if not self._jobs.mark_dead(job, failure.reason):
                return EmbedOutcome(asset_id, CLAIM_LOST, failure.reason, job_settled=True)
            logger.warning(
                "embed job dead asset_id=%s reason=%s attempt=%s",
                asset_id,
                failure.reason,
                job.attempt_count,
            )
            return EmbedOutcome(asset_id, catalog.JOB_DEAD, failure.reason, job_settled=True)
        except TransientFailure as failure:
            return self._schedule_retry(job, asset_id, failure)
        except ObjectStoreUnavailable as failure:
            return self._schedule_retry(
                job,
                asset_id,
                TransientFailure("object_store", str(failure)),
            )
        return outcome

    def _schedule_retry(
        self, job: ClaimedJob, asset_id: str, failure: TransientFailure
    ) -> EmbedOutcome:
        if job.attempts_exhausted:
            if not self._jobs.mark_dead(job, failure.reason):
                return EmbedOutcome(asset_id, CLAIM_LOST, failure.reason, job_settled=True)
            return EmbedOutcome(asset_id, catalog.JOB_DEAD, failure.reason, job_settled=True)
        delay = failure.retry_after
        if delay is None:
            delay = job_module.backoff_seconds(
                job.attempt_count,
                base_seconds=self._settings.ingest_backoff_base_seconds,
                max_seconds=self._settings.ingest_backoff_max_seconds,
            )
        if not self._jobs.retry_later(job, failure.reason, delay):
            return EmbedOutcome(asset_id, CLAIM_LOST, failure.reason, job_settled=True)
        return EmbedOutcome(asset_id, catalog.JOB_RETRY_WAIT, failure.reason, job_settled=True)
