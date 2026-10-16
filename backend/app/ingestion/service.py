"""The ingest pipeline: discover, negative-cache check, rights gate, fetch gate.

Order is the point. Nothing requests image bytes until explicit CC0 evidence has
been written for that record, and a record whose cache is still fresh makes no
request at all.

Progress is recovered from durable state rather than from a stage marker. On a
retry this service asks: is the cache fresh, is there current evidence, does an
asset already exist, are the objects already in the store? Each answer skips
work that actually completed, which is what lets a mid-flight failure resume
without downloading the same bytes twice.
"""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.db import catalog
from app.ingestion import images, met, rights
from app.ingestion.errors import PermanentFailure
from app.ingestion.fetch import ImageFetcher, validate_url
from app.ingestion.met import MetAdapter, MetItem
from app.ingestion.models import IngestOutcome, SourceRecord
from app.ingestion.repository import IngestionRepository
from app.integrations.object_store import (
    ORIGINAL_PREFIX,
    THUMBNAIL_PREFIX,
    ObjectStore,
    content_key,
)
from app.worker import jobs as job_module
from app.worker.jobs import ClaimedJob, JobStore

logger = logging.getLogger("cc0.worker")

SKIPPED_CACHE = "skipped_fresh_cache"
NEGATIVE_CACHED = "negative_cached"
STORED = "stored"
ALREADY_STORED = "already_stored"
RESUMED = "stored_from_existing_objects"

# Pipeline steps, recorded in jobs.payload for the operator. A hint only: the
# retry path still re-reads the catalog and the object store.
STEP_RIGHTS_CHECK = "rights_check"
STEP_DOWNLOAD = "download"
STEP_STORE = "store"


class IngestService:
    def __init__(
        self,
        *,
        settings: Settings,
        adapter: MetAdapter,
        image_fetcher: ImageFetcher,
        object_store: ObjectStore,
        repository: IngestionRepository,
        job_store: JobStore,
        embedding_model_key: str,
        embedding_model_version: str,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._fetcher = image_fetcher
        self._objects = object_store
        self._repository = repository
        self._jobs = job_store
        # The provider's stored identity, which for the fake provider is not
        # MODEL_KEY / MODEL_VERSION.
        self.embedding_model_key = embedding_model_key
        self.embedding_model_version = embedding_model_version
        self._provider_id: UUID | None = None

    @property
    def provider_id(self) -> UUID:
        if self._provider_id is None:
            self._provider_id = self._repository.ensure_provider(
                self._adapter.provider_name,
                self._adapter.base_url,
                self._adapter.policy_url,
                self._settings.ingest_rate_limit_rps,
            )
        return self._provider_id

    # ---- discovery ----

    def discover(self, limit: int | None = None) -> list[str]:
        batch = min(limit or self._settings.ingest_batch_limit, self._settings.ingest_batch_limit)
        return list(self._adapter.discover(batch))

    def enqueue(self, external_ids: list[str]) -> dict[str, str]:
        """Upsert each source record and queue only the work that is still due.

        Returns the records that were skipped, so an operator can see that the
        negative cache did its job instead of inferring it from silence.
        """
        skipped: dict[str, str] = {}
        for external_id in external_ids:
            record = self._repository.upsert_source_record(self.provider_id, external_id)
            if record.is_fresh_negative_cache:
                # No metadata request, no image request, no queued work.
                skipped[external_id] = SKIPPED_CACHE
                continue
            asset = self._repository.get_asset(record.id)
            if asset is not None and asset.status in catalog.ASSET_HAS_BYTES:
                skipped[external_id] = ALREADY_STORED
                continue
            dedupe = job_module.dedupe_key(self._adapter.provider_name, external_id)
            payload = {
                "provider": self._adapter.provider_name,
                "provider_id": str(self.provider_id),
                "external_id": external_id,
            }
            if not self._jobs.enqueue(
                dedupe, payload, max_attempts=self._settings.ingest_max_attempts
            ):
                # The row already exists, so this reuses it rather than queueing
                # a second one. Only a finished job whose negative cache is due
                # again gets a fresh attempt budget; a job that is still pending,
                # waiting out a backoff, leased, or dead is left alone.
                self._jobs.request_recheck_run(dedupe, record.id)
        return skipped

    # ---- pipeline ----

    def execute(
        self, job: ClaimedJob, external_id: str, check_active: Callable[[], None]
    ) -> IngestOutcome:
        provider_id = UUID(str(job.payload["provider_id"]))
        record = self._repository.get_source_record(provider_id, external_id)
        if record is None:
            raise PermanentFailure("source_record_missing", external_id)

        if record.is_fresh_negative_cache:
            return IngestOutcome(external_id, SKIPPED_CACHE, record.eligibility_status)

        asset = self._repository.get_asset(record.id)
        if asset is not None and asset.status in catalog.ASSET_HAS_BYTES:
            return IngestOutcome(external_id, ALREADY_STORED, asset.sha256)

        item: MetItem | None = None
        if not self._has_current_cc0_evidence(record):
            record, item = self._run_rights_gate(job, record, external_id, check_active)
            if record.eligibility_status != catalog.ELIGIBILITY_ELIGIBLE_CC0:
                return IngestOutcome(external_id, NEGATIVE_CACHED, record.eligibility_status)

        return self._store_eligible(job, record, item, external_id, check_active)

    def _has_current_cc0_evidence(self, record: SourceRecord) -> bool:
        """Skip the metadata request when the stored decision still stands.

        A download retry must not re-fetch metadata it already validated, but
        the claim has to come from evidence, not from the record's status alone.
        """
        if record.eligibility_status != catalog.ELIGIBILITY_ELIGIBLE_CC0:
            return False
        if record.recheck_due:
            return False
        evidence = self._repository.latest_evidence(record.id)
        return (
            evidence is not None
            and evidence.rights_type == catalog.RIGHTS_CC0
            and evidence.verification_status == catalog.VERIFICATION_VERIFIED
        )

    def _run_rights_gate(
        self,
        job: ClaimedJob,
        record: SourceRecord,
        external_id: str,
        check_active: Callable[[], None],
    ) -> tuple[SourceRecord, MetItem]:
        self._jobs.merge_payload(job, {"last_step": STEP_RIGHTS_CHECK})
        item = self._adapter.fetch_item(external_id)
        check_active()
        decision = rights.decide(
            item,
            self._adapter.policy_url,
            trusted_public_domain_flag=self._adapter.trusted_public_domain_flag,
        )
        self._repository.record_rights_decision(
            record,
            job=job,
            classification=decision.classification,
            eligibility=decision.eligibility,
            verification_status=decision.verification_status,
            verification_method=decision.verification_method,
            evidence_url=decision.evidence_url,
            raw_statement=decision.raw_statement,
            decision_reason=decision.reason,
            landing_url=item.landing_url,
            original_url=item.original_url,
            raw_metadata=item.raw_metadata,
            recheck_days=self._recheck_days(decision.eligibility),
        )
        logger.info(
            "rights decision external_id=%s classification=%s eligibility=%s reason=%s",
            external_id,
            decision.classification,
            decision.eligibility,
            decision.reason,
        )
        updated = self._repository.get_source_record(record.provider_id, external_id)
        if updated is None:
            raise PermanentFailure("source_record_missing", external_id)
        return updated, item

    def _recheck_days(self, eligibility: str) -> int | None:
        if eligibility == catalog.ELIGIBILITY_UNKNOWN_CACHED:
            return self._settings.ingest_recheck_unknown_days
        if eligibility == catalog.ELIGIBILITY_NON_CC0_CACHED:
            return self._settings.ingest_recheck_non_cc0_days
        # An eligible record has no recheck deadline in M1.
        return None

    def _store_eligible(
        self,
        job: ClaimedJob,
        record: SourceRecord,
        item: MetItem | None,
        external_id: str,
        check_active: Callable[[], None],
    ) -> IngestOutcome:
        url = validate_url(record.original_url, self._settings.image_host_allowlist, what="image")
        if item is None:
            # The rights gate was skipped because its decision still stands, so
            # the asset columns come from the stored allowlisted metadata. This
            # path performs no metadata request. Validating it before the
            # download keeps a record we cannot describe from costing bytes.
            item = met.normalize(external_id, record.raw_metadata)
            if item.invalid_reason is not None:
                raise PermanentFailure("stored_metadata_invalid", item.invalid_reason)

        reused = self._reuse_stored_objects(job.payload)
        if reused is None:
            self._jobs.merge_payload(job, {"last_step": STEP_DOWNLOAD})
            prepared = self._download(url, check_active)
            result = STORED
        else:
            prepared = reused
            result = RESUMED

        check_active()
        self._jobs.merge_payload(job, {"last_step": STEP_STORE, "stored": prepared})
        asset_id = self._repository.store_asset(
            job=job,
            source_record_id=record.id,
            provider_id=record.provider_id,
            creator_name=item.creator_name,
            creator_external_key=item.creator_external_key,
            creator_external_ids=item.creator_external_ids,
            title=item.title,
            description=item.description,
            location_label=item.location_label,
            created_year_start=item.created_year_start,
            created_year_end=item.created_year_end,
            original_key=prepared["original_key"],
            thumbnail_key=prepared["thumbnail_key"],
            sha256=prepared["sha256"],
            mime_type=prepared["mime_type"],
            width=prepared["width"],
            height=prepared["height"],
            model_key=self.embedding_model_key,
            model_version=self.embedding_model_version,
            embed_max_attempts=self._settings.ingest_max_attempts,
        )
        # store_asset committed the job's success alongside the row above.
        if asset_id is None:
            return IngestOutcome(
                external_id, ALREADY_STORED, prepared["sha256"], job_settled=True
            )
        logger.info(
            "asset stored external_id=%s sha256=%s result=%s",
            external_id,
            prepared["sha256"],
            result,
        )
        return IngestOutcome(external_id, result, prepared["sha256"], job_settled=True)

    def _download(self, url: str, check_active: Callable[[], None]) -> dict[str, Any]:
        """The only image request in the pipeline, followed by both object puts."""
        fetched = self._fetcher.fetch(url, guard=check_active)
        processed = images.process(
            fetched.body,
            fetched.content_type,
            max_pixels=self._settings.ingest_max_image_pixels,
            thumbnail_max_pixels=self._settings.ingest_thumbnail_max_pixels,
        )
        check_active()
        original_key = content_key(ORIGINAL_PREFIX, processed.sha256, processed.extension)
        thumbnail_key = content_key(THUMBNAIL_PREFIX, processed.sha256, images.THUMBNAIL_EXTENSION)
        self._objects.put(original_key, processed.original, processed.mime_type, processed.sha256)
        self._objects.put(
            thumbnail_key,
            processed.thumbnail,
            images.THUMBNAIL_CONTENT_TYPE,
            processed.sha256,
        )
        return {
            "sha256": processed.sha256,
            "original_key": original_key,
            "thumbnail_key": thumbnail_key,
            "mime_type": processed.mime_type,
            "width": processed.width,
            "height": processed.height,
        }

    def _reuse_stored_objects(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Verify a previous attempt's objects instead of downloading again.

        The key is content-addressed and the digest is written as object
        metadata, so matching both proves these are the bytes the earlier
        attempt hashed. Anything missing or mismatched falls back to a download.
        """
        hint = payload.get("stored")
        if not isinstance(hint, dict):
            return None
        required = ("sha256", "original_key", "thumbnail_key", "mime_type", "width", "height")
        if any(hint.get(name) in (None, "") for name in required):
            return None
        sha256 = str(hint["sha256"])
        for key in (hint["original_key"], hint["thumbnail_key"]):
            if self._objects.stored_digest(str(key)) != sha256:
                return None
        return {
            "sha256": sha256,
            "original_key": str(hint["original_key"]),
            "thumbnail_key": str(hint["thumbnail_key"]),
            "mime_type": str(hint["mime_type"]),
            "width": int(hint["width"]),
            "height": int(hint["height"]),
        }
