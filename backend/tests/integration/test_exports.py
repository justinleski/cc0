"""Board exports end to end: API, durable job, ZIP contents, rights, retries, lifecycle."""

import csv
import hashlib
import io
import json
import time
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import text

from app.db import catalog
from app.exports.package_service import PackageService
from app.exports.worker_repository import ExportWorkerRepository
from app.integrations.object_store import ObjectStore, ObjectStoreUnavailable
from app.main import create_app
from app.worker.export import ExportWorker
from app.worker.jobs import JobStore
from tests.integration.test_boards import _headers, _public_ids, _settings
from tests.integration.test_search import (
    FIELD,
    HARBOR,
    REEDS,
    ReadyCatalog,
    _publish,
    ready_catalog,
)

_ = ready_catalog


@pytest.fixture(autouse=True)
def empty_private_tables(ready_catalog: ReadyCatalog) -> None:
    with psycopg.connect(ready_catalog.database.migration_url) as connection:
        connection.execute(
            "TRUNCATE board_export_items, board_exports, board_create_operations, "
            "board_items, boards, users CASCADE"
        )


class Harness:
    """An API client plus an export worker over the same throwaway database and bucket."""

    def __init__(self, ready: ReadyCatalog, client: TestClient, settings) -> None:
        self.ready = ready
        self.client = client
        self.settings = settings
        self.alice = _headers(settings, "alice")
        self.bob = _headers(settings, "bob")
        self.objects = ObjectStore(ready._s3, settings.object_store_bucket)
        self.service = PackageService(
            settings=settings,
            repository=ExportWorkerRepository(ready.engine),
            object_store=self.objects,
        )
        self.worker = ExportWorker(
            settings=settings, job_store=JobStore(ready.engine), service=self.service
        )

    def board(self, count: int = 3) -> tuple[str, list[str]]:
        assets = _public_ids(self.client)[:count]
        created = self.client.post("/api/v1/boards", headers=self.alice, json={"name": "Mood"})
        board_id = created.json()["id"]
        for asset_id in assets:
            added = self.client.post(
                f"/api/v1/boards/{board_id}/items", headers=self.alice, json={"asset_id": asset_id}
            )
            assert added.status_code == 201, added.text
        return board_id, assets

    def request(self, board_id: str):
        return self.client.post(f"/api/v1/boards/{board_id}/exports", headers=self.alice)

    def status(self, export_id: str, headers=None):
        return self.client.get(f"/api/v1/exports/{export_id}", headers=headers or self.alice)

    def suppress(self, asset_id: str) -> None:
        source_id = self.ready.scalar(
            "SELECT source_record_id FROM assets WHERE id = :asset_id", asset_id=UUID(asset_id)
        )
        self.ready.ingestion_repository.record_superseding_rights(
            source_id,
            rights_type=catalog.RIGHTS_REJECTED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_OPERATOR_REVIEW,
            evidence_url=None,
            raw_statement="rights withdrawn",
            decision_reason="operator_rejection",
        )

    def download(self, export_id: str) -> zipfile.ZipFile:
        body = self.status(export_id).json()
        assert body["status"] == "READY", body
        response = httpx.get(body["download_url"])
        assert response.status_code == 200
        return zipfile.ZipFile(io.BytesIO(response.content))

    def keys(self) -> list[str]:
        return [key for key, _ in self.objects.list_objects(f"{catalog.EXPORT_PREFIX}/")]


@pytest.fixture
def harness(ready_catalog: ReadyCatalog):
    _publish(ready_catalog, [HARBOR, FIELD, REEDS])
    settings = _settings(ready_catalog).model_copy(
        update={"ingest_max_attempts": 3, "ingest_backoff_base_seconds": 0.01}
    )
    with TestClient(create_app(settings)) as client:
        yield Harness(ready_catalog, client, settings)


def test_export_archive_matches_catalog_and_is_owner_scoped(harness: Harness) -> None:
    board_id, assets = harness.board()
    created = harness.request(board_id)
    assert created.status_code == 202, created.text
    export_id = created.json()["id"]
    assert created.json()["status"] == "QUEUED"
    assert created.json()["download_url"] is None

    # Another account sees nothing: not the board, the export, or the list.
    assert harness.request(board_id).status_code == 202  # owner may export again
    assert harness.client.post(
        f"/api/v1/boards/{board_id}/exports", headers=harness.bob
    ).status_code == 404
    assert harness.status(export_id, harness.bob).status_code == 404
    assert harness.client.get(
        f"/api/v1/boards/{board_id}/exports", headers=harness.bob
    ).status_code == 404
    assert harness.client.get(f"/api/v1/exports/{export_id}").status_code == 401
    assert harness.client.post(f"/api/v1/boards/{board_id}/exports").status_code == 401

    outcomes = harness.worker.drain()
    assert {outcome.result for outcome in outcomes} == {"published"}
    ready = harness.status(export_id).json()
    assert ready["status"] == "READY"
    assert ready["included_count"] == 3 and ready["excluded_count"] == 0
    assert harness.status(export_id, harness.bob).status_code == 404

    archive = harness.download(export_id)
    names = archive.namelist()
    assert names[-2:] == ["attribution.csv", "manifest.json"]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["export_id"] == export_id
    assert manifest["board"] == {"id": board_id, "name": "Mood"}
    assert [entry["asset_id"] for entry in manifest["assets"]] == assets
    rows = list(csv.DictReader(io.StringIO(archive.read("attribution.csv").decode())))
    assert [row["asset_id"] for row in rows] == assets
    for entry, row in zip(manifest["assets"], rows, strict=True):
        body = archive.read(entry["filename"])
        assert hashlib.sha256(body).hexdigest() == entry["sha256"]
        assert row["filename"] == entry["filename"]
        stored = harness.ready.scalar(
            "SELECT sha256 FROM assets WHERE id = :id", id=UUID(entry["asset_id"])
        )
        assert stored == entry["sha256"]
        assert entry["rights"]["type"] == "CC0"
        assert entry["rights"]["verification_status"] == "VERIFIED"
        assert entry["rights"]["verification_method"]
        assert entry["source"]["provider"] and entry["source"]["external_id"]
        assert row["rights_type"] == "CC0"
        assert row["source"] == entry["source"]["provider"]
        assert row["source_landing_url"] == (entry["source"]["landing_url"] or "")
        assert row["original_uri"] == (entry["source"]["original_uri"] or "")
        assert row["license_evidence_url"] == (entry["rights"]["evidence_url"] or "")
        assert row["exported_at"] == manifest["exported_at"]
        assert row["verified_at"] == entry["rights"]["verified_at"]
    assert len(names) == len(assets) + 2

    digest = harness.ready.scalar(
        "SELECT artifact_sha256 FROM board_exports WHERE id = :id", id=UUID(export_id)
    )
    key = harness.ready.scalar(
        "SELECT artifact_key FROM board_exports WHERE id = :id", id=UUID(export_id)
    )
    assert key.endswith(f"/{digest}.zip") and key in harness.keys()
    listing = harness.client.get(f"/api/v1/boards/{board_id}/exports", headers=harness.alice)
    assert [item["id"] for item in listing.json()["items"]][-1] == export_id
    assert all(item["download_url"] is None for item in listing.json()["items"])


def test_snapshot_is_taken_at_request_time(harness: Harness) -> None:
    board_id, assets = harness.board()
    export_id = harness.request(board_id).json()["id"]
    removed = harness.client.delete(
        f"/api/v1/boards/{board_id}/items/{assets[0]}", headers=harness.alice
    )
    assert removed.status_code == 200
    harness.worker.drain()
    manifest = json.loads(harness.download(export_id).read("manifest.json"))
    assert [entry["asset_id"] for entry in manifest["assets"]] == assets


def test_request_validation_empty_board_and_active_cap(harness: Harness) -> None:
    empty = harness.client.post("/api/v1/boards", headers=harness.alice, json={"name": "Empty"})
    assert harness.request(empty.json()["id"]).status_code == 409
    board_id, _ = harness.board()
    statuses = [harness.request(board_id).status_code for _ in range(4)]
    assert statuses == [202, 202, 202, 429]
    jobs = harness.ready.scalar("SELECT count(*) FROM jobs WHERE kind = 'export'")
    assert jobs == 3


def test_suppressed_before_packaging_is_excluded_and_all_suppressed_fails(
    harness: Harness,
) -> None:
    board_id, assets = harness.board()
    export_id = harness.request(board_id).json()["id"]
    harness.suppress(assets[1])
    harness.worker.drain()
    archive = harness.download(export_id)
    manifest = json.loads(archive.read("manifest.json"))
    assert [entry["asset_id"] for entry in manifest["assets"]] == [assets[0], assets[2]]
    assert manifest["excluded"] == [{"asset_id": assets[1], "reason": "rights_changed"}]
    assert not any(assets[1] in name for name in archive.namelist())
    body = harness.status(export_id).json()
    assert (body["included_count"], body["excluded_count"]) == (2, 1)

    other_id = harness.request(board_id).json()["id"]  # board now shows only 2 pins
    harness.suppress(assets[0])
    harness.suppress(assets[2])
    outcome = harness.worker.drain()
    assert [o.result for o in outcome] == ["failed"]
    failed = harness.status(other_id).json()
    assert failed["status"] == "FAILED" and failed["failure_reason"] == "no_eligible_assets"
    assert failed["download_url"] is None
    assert harness.ready.scalar(
        "SELECT state FROM jobs WHERE dedupe_key = :key", key=f"export:{other_id}"
    ) == "dead"
    assert all(other_id not in key for key in harness.keys())


def test_rights_withdrawn_after_publish_blocks_download_and_cleanup_retires_it(
    harness: Harness,
) -> None:
    board_id, assets = harness.board()
    export_id = harness.request(board_id).json()["id"]
    harness.worker.drain()
    assert harness.status(export_id).json()["download_url"]
    harness.suppress(assets[0])
    body = harness.status(export_id).json()
    assert body["status"] == "FAILED" and body["failure_reason"] == "rights_changed"
    assert body["download_url"] is None

    counts = harness.service.cleanup()
    assert counts["rights_withdrawn"] == 1
    assert harness.keys() == []
    assert harness.status(export_id).json()["status"] == "FAILED"


def test_rights_change_during_packaging_retries_and_excludes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_id, assets = harness.board()
    export_id = harness.request(board_id).json()["id"]
    original = harness.service._read_original
    calls = {"count": 0}

    def read_then_suppress(key: str):
        calls["count"] += 1
        if calls["count"] == 1:
            harness.suppress(assets[2])
        return original(key)

    monkeypatch.setattr(harness.service, "_read_original", read_then_suppress)
    first = harness.worker.drain()
    assert [o.result for o in first] == ["retry_wait"]
    assert first[0].detail == "rights_changed_during_packaging"
    assert harness.status(export_id).json()["status"] == "QUEUED"
    assert harness.status(export_id).json()["download_url"] is None

    time.sleep(0.05)
    second = harness.worker.drain()
    assert [o.result for o in second] == ["published"]
    manifest = json.loads(harness.download(export_id).read("manifest.json"))
    assert [entry["asset_id"] for entry in manifest["assets"]] == assets[:2]


def test_interrupted_upload_retries_without_a_partial_archive(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_id, _ = harness.board()
    export_id = harness.request(board_id).json()["id"]
    real_put = harness.objects.put_file
    attempts = {"count": 0}

    def upload_then_die(key, fileobj, content_type, sha256, size):
        attempts["count"] += 1
        if attempts["count"] == 1:
            real_put(key, fileobj, content_type, sha256, size)  # object lands, process "dies"
            raise ObjectStoreUnavailable("connection reset")
        fileobj.seek(0)
        real_put(key, fileobj, content_type, sha256, size)

    monkeypatch.setattr(harness.objects, "put_file", upload_then_die)
    assert [o.result for o in harness.worker.drain()] == ["retry_wait"]
    row = harness.status(export_id).json()
    assert row["status"] == "QUEUED" and row["download_url"] is None
    time.sleep(0.05)
    assert [o.result for o in harness.worker.drain()] == ["published"]
    archive = harness.download(export_id)
    assert archive.testzip() is None
    # Identical bytes share a content-addressed key, so the retry did not fork the artifact.
    assert len(harness.keys()) == 1


def test_attempts_exhausted_marks_export_failed_and_redrive_requeues(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_id, _ = harness.board()
    export_id = harness.request(board_id).json()["id"]

    def down(*_args, **_kwargs):
        raise ObjectStoreUnavailable("EndpointConnectionError")

    monkeypatch.setattr(harness.objects, "put_file", down)
    results = []
    for _ in range(3):
        results += [o.result for o in harness.worker.drain()]
        time.sleep(0.05)
    assert results == ["retry_wait", "retry_wait", "failed"]
    failed = harness.status(export_id).json()
    assert failed["status"] == "FAILED" and failed["failure_reason"] == "object_store"

    monkeypatch.undo()
    assert JobStore(harness.ready.engine).redrive(f"export:{export_id}")
    assert [o.result for o in harness.worker.drain()] == ["published"]
    assert harness.status(export_id).json()["status"] == "READY"


def test_stale_duplicate_execution_cannot_change_a_published_export(harness: Harness) -> None:
    board_id, _ = harness.board()
    export_id = harness.request(board_id).json()["id"]
    jobs = JobStore(harness.ready.engine)
    job = jobs.claim(lease_seconds=300, kind=catalog.JOB_KIND_EXPORT)
    assert job is not None
    assert jobs.claim(lease_seconds=300, kind=catalog.JOB_KIND_EXPORT) is None  # single-flight

    assert harness.worker.run_claimed(job).result == "published"
    before = harness.ready.scalar(
        "SELECT artifact_key FROM board_exports WHERE id = :id", id=UUID(export_id)
    )
    # The same claim presented again, as a duplicate delivery or a zombie worker would.
    assert harness.worker.run_claimed(job).result == "claim_lost"
    after = harness.ready.scalar(
        "SELECT artifact_key FROM board_exports WHERE id = :id", id=UUID(export_id)
    )
    assert before == after and harness.keys() == [before]
    assert harness.worker.drain() == []


def test_expiry_hides_download_and_cleanup_removes_object_and_orphans(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_id, _ = harness.board()
    export_id = harness.request(board_id).json()["id"]
    harness.worker.drain()
    orphan = f"{catalog.EXPORT_PREFIX}/{UUID(int=1)}/{UUID(int=2)}/{'0' * 64}.zip"
    harness.ready._s3.put_object(Bucket=harness.settings.object_store_bucket, Key=orphan, Body=b"x")
    fresh = harness.service.cleanup()
    assert fresh == {"expired": 0, "rights_withdrawn": 0, "orphans": 0}  # inside grace period

    with psycopg.connect(harness.ready.database.migration_url) as connection:
        connection.execute(
            "UPDATE board_exports SET expires_at = now() - interval '1 minute' WHERE id = %s",
            (UUID(export_id),),
        )
    expired = harness.status(export_id).json()
    assert expired["status"] == "EXPIRED" and expired["download_url"] is None

    old = datetime.now(UTC) - timedelta(days=1)
    real_list = harness.objects.list_objects
    monkeypatch.setattr(
        harness.objects, "list_objects", lambda prefix: [(k, old) for k, _ in real_list(prefix)]
    )
    counts = harness.service.cleanup()
    assert counts["expired"] == 1 and counts["orphans"] == 1
    assert harness.keys() == []
    assert harness.status(export_id).json()["status"] == "EXPIRED"
    assert harness.service.cleanup() == {"expired": 0, "rights_withdrawn": 0, "orphans": 0}


def test_dead_lettered_export_job_is_reconciled_by_the_sweep(harness: Harness) -> None:
    board_id, _ = harness.board()
    export_id = harness.request(board_id).json()["id"]
    with harness.ready.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET state = 'dead', dead_at = now(), "
                "last_error = 'attempts_exhausted' WHERE dedupe_key = :key"
            ),
            {"key": f"export:{export_id}"},
        )
    harness.service.sweep()
    body = harness.status(export_id).json()
    assert body["status"] == "FAILED" and body["failure_reason"] == "attempts_exhausted"


def test_api_role_privileges_are_limited(ready_catalog: ReadyCatalog) -> None:
    with psycopg.connect(ready_catalog.database.api_url, autocommit=True) as api:
        for statement in (
            "SELECT * FROM jobs",
            "UPDATE jobs SET state = 'dead'",
            "INSERT INTO jobs (kind, dedupe_key) VALUES ('ingest', 'forged')",
            "UPDATE board_exports SET status = 'READY'",
            "DELETE FROM board_exports",
            "SELECT * FROM rights_evidence",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                api.execute(statement)
        # The one sanctioned path creates no job unless a QUEUED export exists.
        api.execute("SELECT cc0_enqueue_export_job(gen_random_uuid(), 3)")
        assert ready_catalog.scalar("SELECT count(*) FROM jobs WHERE kind = 'export'") == 0
    with psycopg.connect(ready_catalog.database.worker_url, autocommit=True) as worker:
        for statement in ("SELECT * FROM boards", "DELETE FROM board_exports"):
            with pytest.raises(errors.InsufficientPrivilege):
                worker.execute(statement)
