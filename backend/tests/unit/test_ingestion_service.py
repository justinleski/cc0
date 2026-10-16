"""Ingestion orchestration with recorded metadata and no database or network."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db import catalog
from app.ingestion.errors import TransientFailure
from app.ingestion.fixtures import FixtureSource
from app.ingestion.met import MetAdapter
from app.ingestion.models import Asset, Evidence, SourceRecord
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import (
    ALREADY_STORED,
    CLAIM_LOST,
    NEGATIVE_CACHED,
    RESUMED,
    SKIPPED_CACHE,
    STORED,
    IngestService,
)
from app.integrations.object_store import ObjectStore, ObjectStoreUnavailable
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore


@pytest.fixture
def ingestion():
    settings = Settings()
    source = FixtureSource(
        Path(__file__).resolve().parents[2] / "fixtures" / "met",
        settings.metadata_host_allowlist,
        settings.image_host_allowlist,
    )
    repository = Mock(spec=IngestionRepository)
    jobs = Mock(spec=JobStore)
    objects = Mock(spec=ObjectStore)
    record = SourceRecord(
        uuid4(),
        uuid4(),
        "100001",
        None,
        None,
        {},
        catalog.RIGHTS_UNKNOWN,
        catalog.ELIGIBILITY_DISCOVERED,
        None,
        False,
    )
    repository.ensure_provider.return_value = record.provider_id
    repository.get_source_record.return_value = record
    repository.upsert_source_record.return_value = record
    repository.get_asset.return_value = None
    repository.latest_evidence.return_value = None
    repository.store_asset.return_value = uuid4()
    job = ClaimedJob(
        uuid4(),
        "ingest",
        "ingest:met:100001",
        1,
        4,
        {"provider_id": str(record.provider_id), "external_id": record.external_id},
        uuid4(),
    )
    service = IngestService(
        settings=settings,
        adapter=MetAdapter(source),
        image_fetcher=source,
        object_store=objects,
        repository=repository,
        job_store=jobs,
        embedding_model_key="fake",
        embedding_model_version="deterministic-v1",
    )
    return service, repository, jobs, objects, source, record, job


def eligible(repository, source, record):
    item = MetAdapter(source).fetch_item(record.external_id)
    source.requests.clear()
    record = replace(
        record,
        original_url=item.original_url,
        landing_url=item.landing_url,
        raw_metadata=item.raw_metadata,
        eligibility_status=catalog.ELIGIBILITY_ELIGIBLE_CC0,
    )
    repository.get_source_record.return_value = record
    repository.latest_evidence.return_value = Evidence(
        catalog.RIGHTS_CC0,
        catalog.VERIFICATION_VERIFIED,
        catalog.METHOD_PROVIDER_POLICY,
        datetime.now(UTC),
    )
    return record


def test_rights_commit_precedes_image_fetch_and_both_puts_precede_asset_commit(ingestion):
    service, repository, jobs, objects, source, record, job = ingestion
    events = []
    original_fetch = source.fetch

    def commit_rights(current, **decision):
        repository.get_source_record.return_value = replace(
            current,
            eligibility_status=decision["eligibility"],
            original_url=decision["original_url"],
            raw_metadata=decision["raw_metadata"],
        )
        events.append("rights_committed")

    def fetch(*args, **kwargs):
        assert events == ["rights_committed"]
        events.append("image")
        return original_fetch(*args, **kwargs)

    repository.record_rights_decision.side_effect = commit_rights
    source.fetch = fetch
    objects.put.side_effect = lambda *args: events.append("put")
    repository.store_asset.side_effect = (
        lambda **kwargs: events.append("asset_committed") or uuid4()
    )
    assert service.run_claimed(job).result == STORED
    assert events == ["rights_committed", "image", "put", "put", "asset_committed"]
    assert repository.store_asset.call_args.kwargs["model_key"] == "fake"
    assert repository.store_asset.call_args.kwargs["model_version"] == "deterministic-v1"
    jobs.succeed.assert_not_called()  # Repository settled the job atomically.


@pytest.mark.parametrize("status", catalog.CACHED_STATUSES)
def test_fresh_negative_cache_skips_enqueue_and_claimed_work_without_requests(ingestion, status):
    service, repository, jobs, objects, source, record, job = ingestion
    record = replace(record, eligibility_status=status)
    repository.upsert_source_record.return_value = record
    repository.get_source_record.return_value = record
    assert service.enqueue([record.external_id]) == {record.external_id: SKIPPED_CACHE}
    assert service.run_claimed(job).result == SKIPPED_CACHE
    assert source.requests == []
    objects.put.assert_not_called()
    jobs.enqueue.assert_not_called()
    repository.record_rights_decision.assert_not_called()
    jobs.succeed.assert_called_once_with(job)


@pytest.mark.parametrize("status", catalog.ASSET_HAS_BYTES)
def test_existing_asset_skips_all_external_work(ingestion, status):
    service, repository, jobs, objects, source, record, job = ingestion
    repository.get_asset.return_value = Asset(uuid4(), status, "digest", "original", "thumbnail")
    assert service.enqueue([record.external_id]) == {record.external_id: ALREADY_STORED}
    assert service.run_claimed(job).result == ALREADY_STORED
    assert source.requests == []
    objects.put.assert_not_called()
    jobs.enqueue.assert_not_called()


def test_failed_rights_commit_stops_before_image_or_object_requests(ingestion):
    service, repository, jobs, objects, source, _, job = ingestion
    repository.record_rights_decision.side_effect = ClaimLost(job.id)
    assert service.run_claimed(job).result == CLAIM_LOST
    assert len(source.metadata_requests) == 1
    assert source.image_requests == []
    objects.put.assert_not_called()
    repository.store_asset.assert_not_called()
    jobs.succeed.assert_not_called()
    jobs.mark_dead.assert_not_called()
    jobs.retry_later.assert_not_called()


def test_negative_rights_decision_never_fetches_bytes(ingestion):
    service, repository, _, objects, source, record, job = ingestion
    record = replace(record, external_id="200001")
    job = replace(job, payload={**job.payload, "external_id": record.external_id})
    repository.get_source_record.side_effect = [
        record,
        replace(record, eligibility_status=catalog.ELIGIBILITY_NON_CC0_CACHED),
    ]
    assert service.run_claimed(job).result == NEGATIVE_CACHED
    assert repository.record_rights_decision.call_args.kwargs["recheck_days"] == 30
    assert source.image_requests == []
    objects.put.assert_not_called()
    repository.store_asset.assert_not_called()


def test_resume_uses_evidence_and_verified_objects_without_metadata_or_image_request(ingestion):
    service, repository, jobs, objects, source, record, job = ingestion
    eligible(repository, source, record)
    stored = dict(
        sha256="a" * 64,
        original_key="originals/a",
        thumbnail_key="thumbnails/a",
        mime_type="image/jpeg",
        width=1,
        height=1,
    )
    job = replace(job, payload={**job.payload, "stored": stored})
    objects.stored_digest.return_value = stored["sha256"]
    assert service.run_claimed(job).result == RESUMED
    assert source.requests == []
    objects.put.assert_not_called()
    assert objects.stored_digest.call_count == 2
    repository.record_rights_decision.assert_not_called()
    repository.store_asset.assert_called_once()
    jobs.succeed.assert_not_called()


@pytest.mark.parametrize("attempt", [1, 4])
def test_object_failure_preserves_evidence_and_retries_or_exhausts_budget(ingestion, attempt):
    service, repository, jobs, objects, source, record, job = ingestion
    eligible(repository, source, record)
    job = replace(job, attempt_count=attempt)
    objects.put.side_effect = ObjectStoreUnavailable("unavailable")
    outcome = service.run_claimed(job)
    assert outcome.detail == "object_store"
    assert outcome.result == (catalog.JOB_RETRY_WAIT if attempt == 1 else catalog.JOB_DEAD)
    assert source.metadata_requests == []
    assert len(source.image_requests) == 1
    repository.record_rights_decision.assert_not_called()
    repository.store_asset.assert_not_called()
    jobs.succeed.assert_not_called()
    if attempt == 1:
        jobs.retry_later.assert_called_once()
        jobs.mark_dead.assert_not_called()
    else:
        jobs.mark_dead.assert_called_once_with(job, "object_store")
        jobs.retry_later.assert_not_called()


def test_lost_claim_during_retry_does_not_record_a_terminal_state(ingestion):
    service, repository, jobs, _, source, record, job = ingestion
    eligible(repository, source, record)
    source.fetch = Mock(side_effect=TransientFailure("timeout", retry_after=12))
    jobs.retry_later.return_value = False
    assert service.run_claimed(job).result == CLAIM_LOST
    jobs.retry_later.assert_called_once_with(job, "timeout", 12)
    jobs.succeed.assert_not_called()
    jobs.mark_dead.assert_not_called()
