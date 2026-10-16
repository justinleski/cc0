"""Encode originals that ingest already stored.

The job reads object storage. It does not contact the source, and a failure
leaves the asset STORED so search does not publish a row with no vector.
A vector that is already stored for this checkpoint skips inference.
"""

import logging
from uuid import UUID

from app.core.config import Settings
from app.db import catalog
from app.embeddings.models import EmbedOutcome
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.repository import EmbeddingRepository
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.integrations.object_store import ObjectStore
from app.worker.jobs import ClaimedJob, JobStore, embed_dedupe_key

logger = logging.getLogger("cc0.worker.embed")

EMBEDDED = "embedded"
ALREADY_EMBEDDED = "already_embedded"


class EmbedService:
    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        object_store: ObjectStore,
        repository: EmbeddingRepository,
        job_store: JobStore,
    ) -> None:
        self._settings = settings
        self._embeddings = embeddings
        self._objects = object_store
        self._repository = repository
        self._jobs = job_store

    def enqueue_missing(self) -> int:
        """Queue assets that lack the active checkpoint. Dead jobs are redriven.

        Pending and in-flight rows are left alone. Running this twice does not
        create a second job for the same asset and model version.
        """
        queued = 0
        for asset in self._repository.assets_missing_embedding(
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

    @property
    def model_key(self) -> str:
        return self._embeddings.model_key

    @property
    def model_version(self) -> str:
        return self._embeddings.model_version

    def prepare(self) -> None:
        self._embeddings.prepare()

    def execute(self, job: ClaimedJob) -> EmbedOutcome:
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

        asset = self._repository.get_asset_by_id(asset_id)
        if asset is None:
            raise PermanentFailure("asset_missing", "")
        if asset.status not in (catalog.ASSET_STORED, catalog.ASSET_READY):
            raise PermanentFailure("asset_not_embeddable", asset.status)

        if self._repository.embedding_exists(asset_id, model_key, model_version):
            self._repository.publish_existing_embedding(job, asset_id)
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
        self._repository.save_embedding(
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
