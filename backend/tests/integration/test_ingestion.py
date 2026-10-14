"""M1 acceptance, against real Postgres and real RustFS.

The provider is replayed from recorded fixtures, so no test opens a socket to
the network. What these tests prove, in the spec's words: ten verified CC0
fixtures ingest the same way every time; non-CC0 and unknown fixtures leave no
asset row, no image download, no object write, and no embedding write, while
keeping their metadata; a record not yet due for recheck is not fetched again;
running ingestion twice creates no second asset; and a job that fails in the
middle can resume.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.db import catalog
from app.db.engine import create_worker_engine
from app.embeddings.provider import build_provider
from app.ingestion.errors import TransientFailure
from app.ingestion.fetch import FetchedImage
from app.ingestion.fixtures import FixtureSource
from app.ingestion.met import MetAdapter
from app.integrations.object_store import ObjectStore, create_s3_client
from app.worker import ingest as ingest_module
from app.worker.ingest import IngestService
from app.worker.jobs import ClaimLost, JobStore, dedupe_key
from app.worker.store import CatalogStore
from tests.integration.conftest import BACKEND_ROOT, CatalogDatabase

FIXTURE_ROOT = BACKEND_ROOT / "fixtures" / "met"
CC0_IDS = [str(100000 + index) for index in range(1, 11)]
NON_CC0_IDS = ["200001", "200002", "200003"]
UNKNOWN_IDS = ["300001", "300002", "300003"]
# Explicit CC0 whose bytes the fetch gate refuses: no image URL, an off-
# allowlist host, and an HTML body.
REFUSED = {
    "400001": "missing_image_url",
    "400002": "image_host_not_allowed",
    "400003": "not_an_image",
}


class RecordingObjectStore(ObjectStore):
    """Counts writes, so "no object write" is an assertion and not an assumption."""

    def __init__(self, client, bucket: str) -> None:
        super().__init__(client, bucket)
        self.puts: list[str] = []

    def put(self, key: str, body: bytes, content_type: str, sha256: str) -> None:
        super().put(key, body, content_type, sha256)
        self.puts.append(key)


class FlakyFetcher:
    """Fails transiently a fixed number of times, then delegates."""

    def __init__(self, inner: Any, failures: int) -> None:
        self._inner = inner
        self._failures = failures
        self.attempts = 0

    def fetch(self, url: str, *, guard: Any = None) -> FetchedImage:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise TransientFailure("server_error", "503")
        return self._inner.fetch(url, guard=guard)


class SameBytesFetcher:
    """Answers every image URL with one recorded image, as a mirror would."""

    def __init__(self, inner: Any, shared_url: str) -> None:
        self._inner = inner
        self._shared_url = shared_url

    def fetch(self, url: str, *, guard: Any = None) -> FetchedImage:
        fetched = self._inner.fetch(self._shared_url, guard=guard)
        return FetchedImage(url=url, content_type=fetched.content_type, body=fetched.body)


class WorkerKilled(BaseException):
    """Escapes every `except Exception`, the way a SIGKILL skips all cleanup."""


@dataclass
class Harness:
    settings: Settings
    database: CatalogDatabase
    service: IngestService
    source: FixtureSource
    objects: RecordingObjectStore
    job_store: JobStore
    catalog_store: CatalogStore
    engine: Any
    extra_services: list[IngestService] = field(default_factory=list)

    def service_with(self, image_fetcher: Any) -> IngestService:
        """Another service over the same database, with a different fetcher."""
        service = IngestService(
            settings=self.settings,
            adapter=MetAdapter(self.source),
            image_fetcher=image_fetcher,
            object_store=self.objects,
            catalog_store=self.catalog_store,
            job_store=self.job_store,
            embedding_model_key=self.service.embedding_model_key,
            embedding_model_version=self.service.embedding_model_version,
        )
        self.extra_services.append(service)
        return service

    def query(self, sql: str, **params: Any) -> list[tuple]:
        with self.engine.begin() as connection:
            return [tuple(row) for row in connection.execute(text(sql), params)]

    def scalar(self, sql: str, **params: Any) -> Any:
        rows = self.query(sql, **params)
        return rows[0][0] if rows else None

    def job(self, external_id: str) -> Any:
        return self.job_store.get(dedupe_key("met", external_id))

    def drain_until_settled(self, service: IngestService, rounds: int = 6) -> list[Any]:
        """Run retries to completion, since retry_wait carries a short backoff."""
        outcomes: list[Any] = []
        for _ in range(rounds):
            batch = service.drain()
            outcomes.extend(batch)
            if batch and batch[-1].result in (
                ingest_module.STORED,
                ingest_module.RESUMED,
                catalog.JOB_DEAD,
            ):
                break
            time.sleep(0.05)
        return outcomes

    def expire_recheck(self, external_id: str) -> None:
        """Move a negative-cache deadline into the past, as the migrator would."""
        with psycopg.connect(self.database.migration_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE source_records SET next_recheck_at = now() - interval '1 day' "
                "WHERE external_id = %s",
                (external_id,),
            )

    def expire_lease(self, external_id: str) -> None:
        """Let a dead worker's lease run out without waiting for it."""
        with psycopg.connect(self.database.migration_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE jobs SET leased_until = now() - interval '1 second' "
                "WHERE dedupe_key = %s",
                (dedupe_key("met", external_id),),
            )


@pytest.fixture
def harness(
    stack_settings: Settings,
    catalog_database: CatalogDatabase,
    test_bucket: str,
) -> Iterator[Harness]:
    settings = stack_settings.model_copy(
        update={
            "worker_database_url": catalog_database.worker_url,
            "object_store_bucket": test_bucket,
            "ingest_fixture_dir": str(FIXTURE_ROOT),
            # Keep retry waits short enough to drain inside one test.
            "ingest_backoff_base_seconds": 0.01,
            "ingest_backoff_max_seconds": 0.02,
        }
    )
    with psycopg.connect(catalog_database.migration_url, autocommit=True) as connection:
        connection.execute(f"TRUNCATE {', '.join(catalog.TABLES)} CASCADE")

    engine = create_worker_engine(settings)
    client = create_s3_client(settings, timeout_seconds=settings.ingest_request_timeout_seconds)
    source = FixtureSource(
        root=settings.fixture_dir,
        metadata_hosts=settings.metadata_host_allowlist,
        image_hosts=settings.image_host_allowlist,
    )
    objects = RecordingObjectStore(client, settings.object_store_bucket)
    catalog_store = CatalogStore(engine)
    job_store = JobStore(engine)
    embeddings = build_provider(settings)
    harness = Harness(
        settings=settings,
        database=catalog_database,
        service=IngestService(
            settings=settings,
            adapter=MetAdapter(source),
            image_fetcher=source,
            object_store=objects,
            catalog_store=catalog_store,
            job_store=job_store,
            embedding_model_key=embeddings.model_key,
            embedding_model_version=embeddings.model_version,
        ),
        source=source,
        objects=objects,
        job_store=job_store,
        catalog_store=catalog_store,
        engine=engine,
    )
    try:
        yield harness
    finally:
        client.close()
        engine.dispose()


def test_ten_cc0_fixtures_ingest_the_same_way_every_time(harness: Harness) -> None:
    assert harness.service.enqueue(CC0_IDS) == {}
    outcomes = harness.service.drain()

    assert len(outcomes) == 10
    assert {outcome.result for outcome in outcomes} == {ingest_module.STORED}
    assert harness.query("SELECT status, count(*) FROM assets GROUP BY 1") == [
        (catalog.ASSET_STORED, 10)
    ]
    # Ten originals and ten thumbnails, and exactly one image GET per record.
    assert len(harness.objects.puts) == 20
    assert len(harness.source.image_requests) == 10
    assert len(set(harness.source.image_requests)) == 10
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 10
    assert harness.scalar(
        "SELECT count(*) FROM rights_evidence WHERE decision_reason = :reason",
        reason="provider_policy_and_item_flag",
    ) == 10
    assert harness.scalar(
        "SELECT count(*) FROM source_records WHERE eligibility_status = :status",
        status=catalog.ELIGIBILITY_ELIGIBLE_CC0,
    ) == 10

    stored = harness.query(
        "SELECT sha256, original_key, thumbnail_key, mime_type, width, height FROM assets"
    )
    for sha256, original_key, thumbnail_key, mime_type, width, height in stored:
        assert original_key == f"originals/{sha256[:2]}/{sha256[2:4]}/{sha256}.jpg"
        assert thumbnail_key == f"thumbnails/{sha256[:2]}/{sha256[2:4]}/{sha256}.webp"
        assert (mime_type, width > 0, height > 0) == ("image/jpeg", True, True)
        assert harness.objects.stored_digest(original_key) == sha256
        assert harness.objects.stored_digest(thumbnail_key) == sha256

    # A second run of the same catalog.
    harness.source.requests.clear()
    harness.objects.puts.clear()
    repeat = harness.service.enqueue(CC0_IDS)

    assert set(repeat.values()) == {ingest_module.ALREADY_STORED}
    assert harness.service.drain() == []
    # No metadata request and no image request: the stored asset answers both.
    assert harness.source.requests == []
    assert harness.objects.puts == []
    assert harness.scalar("SELECT count(*) FROM assets") == 10
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 10


def test_non_cc0_and_unknown_records_never_reach_an_image(harness: Harness) -> None:
    harness.service.enqueue(NON_CC0_IDS + UNKNOWN_IDS)
    outcomes = harness.service.drain()

    assert {outcome.result for outcome in outcomes} == {ingest_module.NEGATIVE_CACHED}
    # No asset row, no image download, no object write.
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    assert harness.source.image_requests == []
    assert harness.objects.puts == []
    # The embeddings table exists, and a non-CC0 record still does not get a row.
    assert harness.scalar(f"SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}") == 0
    assert harness.scalar(
        "SELECT count(*) FROM jobs WHERE kind = :kind",
        kind=catalog.JOB_KIND_EMBED,
    ) == 0

    rows = harness.query(
        """
        SELECT external_id, eligibility_status, rights_classification,
               next_recheck_at IS NOT NULL, landing_url IS NOT NULL,
               raw_metadata ? 'title'
        FROM source_records ORDER BY external_id
        """
    )
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("200001", catalog.ELIGIBILITY_NON_CC0_CACHED, catalog.RIGHTS_REJECTED),
        ("200002", catalog.ELIGIBILITY_NON_CC0_CACHED, catalog.RIGHTS_OTHER_OPEN),
        ("200003", catalog.ELIGIBILITY_NON_CC0_CACHED, catalog.RIGHTS_PUBLIC_DOMAIN_MARK),
        ("300001", catalog.ELIGIBILITY_UNKNOWN_CACHED, catalog.RIGHTS_UNKNOWN),
        ("300002", catalog.ELIGIBILITY_UNKNOWN_CACHED, catalog.RIGHTS_UNKNOWN),
        ("300003", catalog.ELIGIBILITY_UNKNOWN_CACHED, catalog.RIGHTS_UNKNOWN),
    ]
    # Every one keeps a recheck deadline and minimal metadata. 300003 has no
    # usable landing page, which is why its metadata was rejected at all.
    assert all(row[3] for row in rows)
    assert [row[4] for row in rows] == [True] * 5 + [False]
    assert all(row[5] for row in rows)

    evidence = harness.query(
        """
        SELECT source_records.external_id, rights_evidence.verification_status,
               rights_evidence.raw_statement <> '', rights_evidence.asset_id IS NULL
        FROM rights_evidence JOIN source_records
          ON source_records.id = rights_evidence.source_record_id
        ORDER BY source_records.external_id
        """
    )
    assert len(evidence) == 6
    assert {row[1] for row in evidence} == {
        catalog.VERIFICATION_REJECTED,
        catalog.VERIFICATION_UNVERIFIED,
    }
    # Evidence attaches to the source record even though no asset exists.
    assert all(row[2] and row[3] for row in evidence)


def test_a_record_not_due_for_recheck_is_not_fetched_again(harness: Harness) -> None:
    harness.service.enqueue(NON_CC0_IDS)
    harness.service.drain()
    harness.source.requests.clear()

    skipped = harness.service.enqueue(NON_CC0_IDS)

    assert set(skipped.values()) == {ingest_module.SKIPPED_CACHE}
    # The cache short-circuits before any request, metadata included.
    assert harness.source.requests == []
    assert harness.service.drain() == []
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 3


def test_a_due_recheck_appends_evidence_without_rewriting_the_last_answer(
    harness: Harness,
) -> None:
    harness.service.enqueue(["200001"])
    harness.service.drain()
    harness.expire_recheck("200001")
    harness.source.requests.clear()

    assert harness.service.enqueue(["200001"]) == {}
    outcomes = harness.service.drain()

    assert [outcome.result for outcome in outcomes] == [ingest_module.NEGATIVE_CACHED]
    # One new metadata request, and still no image request.
    assert len(harness.source.metadata_requests) == 1
    assert harness.source.image_requests == []
    # The history grew instead of being overwritten.
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 2
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    assert harness.job("200001").attempt_count == 1


def test_a_transient_failure_retries_and_still_stores_one_object(harness: Harness) -> None:
    flaky = FlakyFetcher(harness.source, failures=2)
    service = harness.service_with(flaky)
    service.enqueue(["100001"])

    outcomes = harness.drain_until_settled(service)

    assert outcomes[-1].result == ingest_module.STORED
    assert [outcome.result for outcome in outcomes[:-1]] == [catalog.JOB_RETRY_WAIT] * 2
    assert flaky.attempts == 3
    # Only the successful attempt downloaded and wrote.
    assert len(harness.source.image_requests) == 1
    assert len(harness.objects.puts) == 2
    job = harness.job("100001")
    assert (job.state, job.attempt_count, job.last_error) == (catalog.JOB_SUCCEEDED, 3, None)
    assert harness.scalar("SELECT count(*) FROM assets") == 1


def test_exhausted_retries_dead_letter_and_keep_the_cc0_decision(harness: Harness) -> None:
    service = harness.service_with(FlakyFetcher(harness.source, failures=99))
    service.enqueue(["100002"])

    outcomes = harness.drain_until_settled(service)

    assert outcomes[-1].result == catalog.JOB_DEAD
    job = harness.job("100002")
    assert (job.state, job.attempt_count, job.last_error) == (
        catalog.JOB_DEAD,
        harness.settings.ingest_max_attempts,
        "server_error",
    )
    # A failed fetch is a job outcome, never a rights outcome.
    assert harness.scalar("SELECT eligibility_status FROM source_records") == (
        catalog.ELIGIBILITY_ELIGIBLE_CC0
    )
    assert harness.query(
        "SELECT rights_type, verification_status FROM rights_evidence"
    ) == [(catalog.RIGHTS_CC0, catalog.VERIFICATION_VERIFIED)]
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    assert harness.objects.puts == []


@pytest.mark.parametrize(("external_id", "reason"), sorted(REFUSED.items()))
def test_a_refused_image_is_dead_on_the_first_attempt(
    harness: Harness,
    external_id: str,
    reason: str,
) -> None:
    harness.service.enqueue([external_id])
    outcomes = harness.service.drain()

    assert [(outcome.result, outcome.detail) for outcome in outcomes] == [
        (catalog.JOB_DEAD, reason)
    ]
    job = harness.job(external_id)
    # No transient budget is spent on an answer that cannot change.
    assert (job.state, job.attempt_count, job.last_error) == (catalog.JOB_DEAD, 1, reason)
    assert harness.scalar("SELECT eligibility_status FROM source_records") == (
        catalog.ELIGIBILITY_ELIGIBLE_CC0
    )
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    assert harness.objects.puts == []


def test_a_repeat_ingest_leaves_a_dead_job_dead_and_redrive_clears_it(harness: Harness) -> None:
    failing = harness.service_with(FlakyFetcher(harness.source, failures=99))
    failing.enqueue(["100004"])
    harness.drain_until_settled(failing)
    assert harness.job("100004").state == catalog.JOB_DEAD

    # Re-running ingestion must not quietly clear a failure nobody has seen.
    harness.service.enqueue(["100004"])
    assert harness.service.drain() == []
    assert harness.job("100004").state == catalog.JOB_DEAD

    assert harness.job_store.redrive(dedupe_key("met", "100004")) is True
    revived = harness.job("100004")
    assert (revived.state, revived.attempt_count) == (catalog.JOB_PENDING, 0)
    # The working fetcher now completes the same job row.
    outcomes = harness.service.drain()
    assert [outcome.result for outcome in outcomes] == [ingest_module.STORED]
    assert harness.scalar("SELECT count(*) FROM assets") == 1
    # The rights gate was not re-run: the stored decision still stands.
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 1


def test_a_crash_after_the_object_write_resumes_without_a_second_download(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crashed = {"done": False}
    original = harness.catalog_store.store_asset

    def crash_once(**kwargs):
        if not crashed["done"]:
            crashed["done"] = True
            # Both objects are already written at this point, and the asset row
            # is not, which is exactly the window a worker can die in.
            raise TransientFailure("object_store", "simulated crash before commit")
        return original(**kwargs)

    monkeypatch.setattr(harness.catalog_store, "store_asset", crash_once)
    harness.service.enqueue(["100003"])

    first = harness.service.drain()
    assert [outcome.result for outcome in first] == [catalog.JOB_RETRY_WAIT]
    assert len(harness.objects.puts) == 2
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    # The digest and keys were recorded for the next attempt to verify.
    assert set(harness.job("100003").payload["stored"]) == {
        "sha256",
        "original_key",
        "thumbnail_key",
        "mime_type",
        "width",
        "height",
    }

    time.sleep(0.05)
    second = harness.service.drain()

    assert [outcome.result for outcome in second] == [ingest_module.RESUMED]
    # One image GET and two object writes across both attempts.
    assert len(harness.source.image_requests) == 1
    assert len(harness.objects.puts) == 2
    assert harness.scalar("SELECT count(*) FROM assets") == 1
    assert harness.job("100003").state == catalog.JOB_SUCCEEDED


def test_a_killed_worker_resumes_after_its_lease_expires_without_a_second_download(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = harness.catalog_store.store_asset

    def killed(**kwargs):
        raise WorkerKilled

    # Both objects are written and the asset row is not: the process dies with
    # no chance to record a retry, so the row is left leased.
    monkeypatch.setattr(harness.catalog_store, "store_asset", killed)
    harness.service.enqueue(["100006"])
    with pytest.raises(WorkerKilled):
        harness.service.drain()
    assert harness.job("100006").state == catalog.JOB_LEASED
    assert len(harness.objects.puts) == 2

    # A live lease is not taken from a worker that may still be running.
    monkeypatch.setattr(harness.catalog_store, "store_asset", original)
    assert harness.service.drain() == []

    harness.expire_lease("100006")
    outcomes = harness.service.drain()

    assert [outcome.result for outcome in outcomes] == [ingest_module.RESUMED]
    assert len(harness.source.image_requests) == 1
    assert len(harness.objects.puts) == 2
    assert harness.scalar("SELECT count(*) FROM assets") == 1
    job = harness.job("100006")
    # The reclaim spent an attempt, which is what bounds a poison job.
    assert (job.state, job.attempt_count) == (catalog.JOB_SUCCEEDED, 2)


def test_a_job_that_keeps_killing_the_worker_is_dead_lettered(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killed(**kwargs):
        raise WorkerKilled

    monkeypatch.setattr(harness.catalog_store, "store_asset", killed)
    harness.service.enqueue(["100007"])
    for _ in range(harness.settings.ingest_max_attempts):
        with pytest.raises(WorkerKilled):
            harness.service.drain()
        harness.expire_lease("100007")

    # The last lease expired with no attempts left. The claim query can no
    # longer see the row, so the reaper is what makes it visible as dead.
    assert harness.service.drain() == []
    job = harness.job("100007")
    assert (job.state, job.attempt_count, job.last_error) == (
        catalog.JOB_DEAD,
        harness.settings.ingest_max_attempts,
        "attempts_exhausted",
    )
    assert harness.scalar("SELECT eligibility_status FROM source_records") == (
        catalog.ELIGIBILITY_ELIGIBLE_CC0
    )
    assert harness.scalar("SELECT count(*) FROM assets") == 0
    # Later attempts reused the verified objects instead of downloading again.
    assert len(harness.source.image_requests) == 1


def test_one_worker_at_a_time_holds_a_job(harness: Harness) -> None:
    harness.service.enqueue(["100005"])

    first = harness.job_store.claim(lease_seconds=300)
    second = harness.job_store.claim(lease_seconds=300)

    # FOR UPDATE SKIP LOCKED plus the lease: a duplicate delivery or a second
    # worker cannot start the same download.
    assert first is not None
    assert second is None
    assert harness.job("100005").state == catalog.JOB_LEASED


def test_creators_are_reused_within_a_provider(harness: Harness) -> None:
    # The first two fixtures share one artist with an authority URL.
    harness.service.enqueue(["100001", "100002"])
    harness.service.drain()

    creators = harness.query("SELECT external_key, name FROM creators")
    assert len(creators) == 1
    assert creators[0][0].startswith("ulan:")
    assert harness.scalar("SELECT count(DISTINCT creator_id) FROM assets") == 1


def test_identical_bytes_under_two_records_keep_two_assets(harness: Harness) -> None:
    """Shared bytes are storage reuse, not a claim that two works are the same."""
    recorded = json.loads((FIXTURE_ROOT / "objects" / f"{CC0_IDS[0]}.json").read_text())
    service = harness.service_with(SameBytesFetcher(harness.source, recorded["primaryImage"]))
    service.enqueue(CC0_IDS[:2])

    outcomes = service.drain()

    assert [outcome.result for outcome in outcomes] == [ingest_module.STORED] * 2
    rows = harness.query("SELECT source_record_id, sha256, original_key FROM assets")
    # One digest and one object key, but two asset rows with their own evidence,
    # because sha256 is deliberately not unique.
    assert len(rows) == 2
    assert len({row[0] for row in rows}) == 2
    assert len({row[1] for row in rows}) == 1
    assert len({row[2] for row in rows}) == 1
    assert harness.scalar("SELECT count(DISTINCT source_record_id) FROM rights_evidence") == 2


def test_a_repeat_ingest_keeps_a_retry_budget(harness: Harness) -> None:
    """Re-running ingest must not erase backoff or attempts already spent."""
    service = harness.service_with(FlakyFetcher(harness.source, failures=1))
    service.enqueue(["100008"])
    first = service.drain()

    assert [outcome.result for outcome in first] == [catalog.JOB_RETRY_WAIT]
    waiting = harness.job("100008")
    assert waiting.attempt_count == 1
    with psycopg.connect(harness.database.migration_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE jobs SET next_run_at = now() + interval '1 hour' WHERE dedupe_key = %s",
            (dedupe_key("met", "100008"),),
        )

    harness.service.enqueue(["100008"])

    again = harness.job("100008")
    assert (again.state, again.attempt_count) == (catalog.JOB_RETRY_WAIT, 1)
    # Still waiting out the hour, so nothing is claimed.
    assert harness.service.drain() == []
    assert harness.job("100008").state == catalog.JOB_RETRY_WAIT


def test_a_due_recheck_resets_a_succeeded_negative_cache_job(harness: Harness) -> None:
    harness.service.enqueue(["200001"])
    harness.service.drain()
    finished = harness.job("200001")
    assert (finished.state, finished.attempt_count) == (catalog.JOB_SUCCEEDED, 1)

    harness.expire_recheck("200001")
    assert harness.service.enqueue(["200001"]) == {}

    reset = harness.job("200001")
    assert (reset.state, reset.attempt_count) == (catalog.JOB_PENDING, 0)


def test_a_stale_claim_cannot_overwrite_the_worker_that_reclaimed_it(harness: Harness) -> None:
    harness.service.enqueue(["100009"])
    first = harness.job_store.claim(lease_seconds=300)
    assert first is not None
    harness.expire_lease("100009")
    second = harness.job_store.claim(lease_seconds=300)

    assert second is not None
    assert first.claim_token != second.claim_token
    assert harness.job_store.mark_dead(first, "old_worker_failure") is False
    assert harness.job_store.retry_later(first, "old_worker_failure", 30) is False
    # The reclaim stands. The expired worker wrote nothing.
    current = harness.job("100009")
    assert (current.state, current.last_error) == (catalog.JOB_LEASED, None)

    record = harness.catalog_store.get_source_record(
        UUID(str(first.payload["provider_id"])),
        "100009",
    )
    assert record is not None
    with pytest.raises(ClaimLost):
        harness.catalog_store.record_rights_decision(
            record,
            job=first,
            classification=catalog.RIGHTS_CC0,
            eligibility=catalog.ELIGIBILITY_ELIGIBLE_CC0,
            verification_status=catalog.VERIFICATION_VERIFIED,
            verification_method=catalog.METHOD_PROVIDER_POLICY,
            evidence_url=None,
            raw_statement="stale",
            decision_reason="stale",
            landing_url=None,
            original_url=None,
            raw_metadata={},
            recheck_days=None,
        )
    assert harness.scalar("SELECT count(*) FROM rights_evidence") == 0

    assert harness.job_store.mark_dead(second, "current_worker") is True
    dead = harness.job("100009")
    assert (dead.state, dead.last_error) == (catalog.JOB_DEAD, "current_worker")


def test_an_asset_commit_and_its_success_are_one_claim(harness: Harness) -> None:
    """A lost claim cannot insert an asset, and a stored one is not dead-lettered."""
    harness.service.enqueue(["100010"])
    outcomes = harness.service.drain()
    assert [outcome.result for outcome in outcomes] == [ingest_module.STORED]
    assert harness.job("100010").state == catalog.JOB_SUCCEEDED

    # Pretend the lease expired on the last attempt after the asset existed.
    with psycopg.connect(harness.database.migration_url, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE jobs SET
                state = 'leased',
                attempt_count = %s,
                leased_until = now() - interval '1 second',
                claim_token = gen_random_uuid()
            WHERE dedupe_key = %s
            """,
            (harness.settings.ingest_max_attempts, dedupe_key("met", "100010")),
        )

    assert harness.service.drain() == []
    assert harness.job("100010").state == catalog.JOB_SUCCEEDED
    assert harness.scalar("SELECT count(*) FROM assets") == 1


def test_fixture_catalog_is_the_recorded_shape(harness: Harness) -> None:
    discovered = harness.service.discover(100)

    assert discovered[:10] == CC0_IDS
    assert set(NON_CC0_IDS + UNKNOWN_IDS + list(REFUSED)) <= set(discovered)
    assert Path(harness.settings.fixture_dir, "catalog.json").exists()
    # Discovery reads the catalog only. No item metadata, no image.
    assert len(harness.source.metadata_requests) == 1
    assert harness.source.image_requests == []
