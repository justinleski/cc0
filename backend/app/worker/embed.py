"""Encode originals that ingest already stored.

The job reads object storage. It does not contact the source, and a failure
leaves the asset STORED so search does not publish a row with no vector.
A vector that is already stored for this checkpoint skips inference.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.core.config import Settings
from app.db import catalog
from app.embeddings.provider import EmbeddingProvider
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.integrations.object_store import ObjectStore, ObjectStoreUnavailable
from app.worker import jobs as job_module
from app.worker.jobs import ClaimLost, JobStore, embed_dedupe_key
from app.worker.lease import LeaseKeeper
from app.worker.store import CatalogStore

logger = logging.getLogger("cc0.worker.embed")

EMBEDDED = "embedded"
ALREADY_EMBEDDED = "already_embedded"
CLAIM_LOST = "claim_lost"


@dataclass(frozen=True)
class EmbedOutcome:
    asset_id: str
    result: str
    detail: str = ""
    job_settled: bool = False


class EmbedService:
    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        object_store: ObjectStore,
        catalog_store: CatalogStore,
        job_store: JobStore,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._objects = object_store
        self._catalog = catalog_store
        self._jobs = job_store

    def enqueue_missing(self) -> int:
        """Queue assets that lack the active checkpoint. Dead jobs are redriven.

        Pending and in-flight rows are left alone. Running this twice does not
        create a second job for the same asset and model version.
        """
        queued = 0
        for asset in self._catalog.assets_missing_embedding(
            self._embeddings.model_key,
            self._embeddings.model_version,
        ):
            dedupe = embed_dedupe_key(
                self._embeddings.model_key,
                self._embeddings.model_version,
                asset.id,
            )
            created = self._jobs.enqueue(
                dedupe,
                {
                    "asset_id": str(asset.id),
                    "model_key": self._embeddings.model_key,
                    "model_version": self._embeddings.model_version,
                    "original_key": asset.original_key,
                },
                max_attempts=self._settings.ingest_max_attempts,
                kind=catalog.JOB_KIND_EMBED,
            )
            if created or self._jobs.redrive(dedupe):
                queued += 1
        return queued

    def drain(self, max_jobs: int | None = None) -> list[EmbedOutcome]:
        self._embeddings.prepare()
        self._jobs.reap_exhausted(kind=catalog.JOB_KIND_EMBED)
        outcomes: list[EmbedOutcome] = []
        budget = max_jobs if max_jobs is not None else self._settings.ingest_batch_limit
        for _ in range(budget):
            job = self._jobs.claim(
                lease_seconds=self._settings.ingest_lease_seconds,
                kind=catalog.JOB_KIND_EMBED,
                model_key=self._embeddings.model_key,
                model_version=self._embeddings.model_version,
            )
            if job is None:
                break
            outcomes.append(self.run_claimed(job))
        return outcomes

    def run_claimed(self, job) -> EmbedOutcome:
        asset_id = str(job.payload.get("asset_id", ""))
        try:
            with LeaseKeeper(
                self._jobs,
                job,
                lease_seconds=self._settings.ingest_lease_seconds,
                deadline_seconds=self._settings.ingest_job_deadline_seconds,
            ):
                outcome = self._execute(job)
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

    def _execute(self, job) -> EmbedOutcome:
        asset_raw = job.payload.get("asset_id")
        model_key = str(job.payload.get("model_key") or "")
        model_version = str(job.payload.get("model_version") or "")
        original_key = str(job.payload.get("original_key") or "")
        if not asset_raw or not model_key or not model_version or not original_key:
            raise PermanentFailure("embed_payload_invalid", "")
        try:
            asset_id = UUID(str(asset_raw))
        except ValueError as exc:
            raise PermanentFailure("embed_payload_invalid", "") from exc
        if (model_key, model_version) != (
            self._embeddings.model_key,
            self._embeddings.model_version,
        ):
            # The job was queued for a different checkpoint than this process
            # has loaded. Writing it with the loaded model would label the
            # vector as something it is not.
            raise PermanentFailure("embed_model_mismatch", "")

        asset = self._catalog.get_asset_by_id(asset_id)
        if asset is None:
            raise PermanentFailure("asset_missing", "")
        if asset.status not in (catalog.ASSET_STORED, catalog.ASSET_READY):
            raise PermanentFailure("asset_not_embeddable", asset.status)

        if self._catalog.embedding_exists(asset_id, model_key, model_version):
            self._catalog.publish_existing_embedding(job, asset_id)
            return EmbedOutcome(str(asset_id), ALREADY_EMBEDDED, job_settled=True)

        body = self._objects.get_bytes(original_key)
        if body is None:
            raise PermanentFailure("original_missing", "")
        try:
            vector = self._embeddings.embed_image(body)
        except Exception as exc:
            raise TransientFailure("embedding_failed", type(exc).__name__) from exc
        if len(vector) != self._embeddings.dimensions:
            raise PermanentFailure("embedding_dimensions", str(len(vector)))
        self._catalog.save_embedding(
            job=job,
            asset_id=asset_id,
            model_key=model_key,
            model_version=model_version,
            dimensions=len(vector),
            vector=vector,
        )
        logger.info(
            "asset embedded asset_id=%s model=%s version=%s",
            asset_id,
            model_key,
            model_version,
        )
        return EmbedOutcome(str(asset_id), EMBEDDED, job_settled=True)

    def _schedule_retry(self, job, asset_id: str, failure: TransientFailure) -> EmbedOutcome:
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
