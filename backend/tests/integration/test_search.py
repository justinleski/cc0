"""Embeddings publish STORED assets, and search reads only READY rows.

The provider is the deterministic fake. These tests check filters, required
phrases, grants, and ordering against vectors the test can recompute. They do
not load OpenCLIP.
"""

from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.db import catalog
from app.db.engine import create_worker_engine
from app.embeddings.provider import FAKE_MODEL_KEY, FAKE_MODEL_VERSION, build_provider
from app.ingestion.fixtures import FixtureSource
from app.ingestion.met import MetAdapter
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestService
from app.integrations.object_store import ObjectStore, create_s3_client
from app.main import create_app
from app.search.cursor import decode_cursor, encode_cursor
from app.worker.embed import ALREADY_EMBEDDED, EMBEDDED, EmbedService
from app.worker.jobs import JobStore
from app.worker.store import CatalogStore
from tests.integration.conftest import BACKEND_ROOT, CatalogDatabase

FIXTURE_ROOT = BACKEND_ROOT / "fixtures" / "met"
HARBOR = "100001"
REEDS = "100002"
FIELD = "100004"
OPENCLIP_KEY = "ViT-B-32"
OPENCLIP_VERSION = "laion2b_s34b_b79k"


class ReadyCatalog:
    def __init__(self, settings: Settings, database: CatalogDatabase, client) -> None:
        self.settings = settings
        self.database = database
        self.engine = create_worker_engine(settings)
        self._s3 = client
        source = FixtureSource(
            root=settings.fixture_dir,
            metadata_hosts=settings.metadata_host_allowlist,
            image_hosts=settings.image_host_allowlist,
        )
        objects = ObjectStore(client, settings.object_store_bucket)
        catalog_store = CatalogStore(self.engine)
        self.ingestion_repository = IngestionRepository(self.engine)
        job_store = JobStore(self.engine)
        embeddings = build_provider(settings)
        self.embeddings = embeddings
        self.ingest = IngestService(
            settings=settings,
            adapter=MetAdapter(source),
            image_fetcher=source,
            object_store=objects,
            repository=self.ingestion_repository,
            job_store=job_store,
            embedding_model_key=embeddings.model_key,
            embedding_model_version=embeddings.model_version,
        )
        self.embed = EmbedService(
            settings=settings,
            embeddings=embeddings,
            object_store=objects,
            catalog_store=catalog_store,
            job_store=job_store,
        )
        self.job_store = job_store
        self.catalog = catalog_store

    def close(self) -> None:
        self._s3.close()
        self.engine.dispose()

    def scalar(self, sql: str, **params) -> object:
        with self.engine.begin() as connection:
            row = connection.execute(text(sql), params).one_or_none()
        return None if row is None else row[0]


@pytest.fixture
def ready_catalog(
    stack_settings: Settings,
    catalog_database: CatalogDatabase,
    test_bucket: str,
) -> Iterator[ReadyCatalog]:
    settings = stack_settings.model_copy(
        update={
            "worker_database_url": catalog_database.worker_url,
            "database_url": catalog_database.api_url,
            "object_store_bucket": test_bucket,
            "ingest_fixture_dir": str(FIXTURE_ROOT),
            "embedding_provider": "fake",
            "model_key": "ViT-B-32",
            "model_version": "laion2b_s34b_b79k",
            "ingest_backoff_base_seconds": 0.01,
            "ingest_backoff_max_seconds": 0.02,
        }
    )
    with psycopg.connect(catalog_database.migration_url, autocommit=True) as connection:
        connection.execute(f"TRUNCATE {', '.join(catalog.TABLES)} CASCADE")
    client = create_s3_client(settings, timeout_seconds=settings.ingest_request_timeout_seconds)
    catalog_under_test = ReadyCatalog(settings, catalog_database, client)
    try:
        yield catalog_under_test
    finally:
        catalog_under_test.close()


def _publish(catalog_under_test: ReadyCatalog, external_ids: list[str]) -> None:
    assert catalog_under_test.ingest.enqueue(external_ids) == {}
    catalog_under_test.ingest.drain()
    outcomes = catalog_under_test.embed.drain()
    assert {outcome.result for outcome in outcomes} == {EMBEDDED}
    assert len(outcomes) == len(external_ids)


def test_embed_publishes_ready_and_a_repeat_does_not_write_a_second_vector(
    ready_catalog: ReadyCatalog,
) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])

    assert ready_catalog.scalar(
        "SELECT count(*) FROM assets WHERE status = :ready",
        ready=catalog.ASSET_READY,
    ) == 2
    assert ready_catalog.scalar(f"SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}") == 2
    assert ready_catalog.scalar(
        f"SELECT min(dimensions) FROM {catalog.EMBEDDINGS_TABLE}"
    ) == 512

    again = ready_catalog.embed.drain()
    assert again == []
    ready_catalog.embed.enqueue_missing()
    repeated = ready_catalog.embed.drain()
    assert repeated == [] or {outcome.result for outcome in repeated} <= {ALREADY_EMBEDDED}
    assert ready_catalog.scalar(f"SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}") == 2


def test_search_requires_quotes_filters_exactly_and_hides_the_negative_cache(
    ready_catalog: ReadyCatalog,
) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])
    with psycopg.connect(ready_catalog.database.api_url) as connection:
        connection.execute(f"SELECT asset_id FROM {catalog.READY_ASSETS_VIEW}")
    with psycopg.connect(ready_catalog.database.api_url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT landing_url FROM source_records")
    with psycopg.connect(ready_catalog.database.api_url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(f"UPDATE {catalog.EMBEDDINGS_TABLE} SET model_key = model_key")

    with TestClient(create_app(ready_catalog.settings)) as client:
        quoted = client.get("/api/v1/assets", params={"q": '"Harbor at Dawn"'})
        assert quoted.status_code == 200
        body = quoted.json()
        assert body["applied_query"] == '"Harbor at Dawn"'
        assert [item["title"] for item in body["items"]] == ["Harbor at Dawn"]

        stray = client.get("/api/v1/assets", params={"q": '"Harbor'})
        assert stray.status_code == 200
        assert stray.json()["applied_query"] == "Harbor"

        creator_id = body["items"][0]["creator_id"]
        by_creator = client.get("/api/v1/assets", params={"creator_id": creator_id})
        assert [item["title"] for item in by_creator.json()["items"]] == ["Harbor at Dawn"]

        later = client.get("/api/v1/assets", params={"year_from": 1900, "year_to": 2000})
        assert "Harbor at Dawn" not in [item["title"] for item in later.json()["items"]]
        early = client.get(
            "/api/v1/assets",
            params={"location": "Edo, Japan", "year_from": 1800, "year_to": 1840},
        )
        assert [item["title"] for item in early.json()["items"]] == ["Harbor at Dawn"]

        detail = client.get(f"/api/v1/assets/{body['items'][0]['id']}")
        assert detail.status_code == 200
        assert detail.json()["rights_type"] == "CC0"
        assert detail.json()["landing_url"].endswith("/100001")
        assert detail.json()["verified_at"]

        missing = client.get("/api/v1/assets/00000000-0000-0000-0000-000000000099")
        assert missing.status_code == 404

        related = client.get(f"/api/v1/assets/{body['items'][0]['id']}/related")
        assert related.status_code == 200
        assert [item["title"] for item in related.json()["items"]] == ["Field in Late Summer"]

        creators = client.get("/api/v1/creators", params={"query": "Vincent van Goh"})
        assert creators.status_code == 200
        assert creators.json()["did_you_mean"] == "Vincent van Gogh"

        # A required phrase that no asset contains stays empty. An unquoted
        # nonsense string would still have nearest neighbors.
        empty = client.get("/api/v1/assets", params={"q": '"zzzz-not-a-title"'})
        assert empty.json()["items"] == []
        assert empty.json()["applied_query"] == '"zzzz-not-a-title"'


def test_a_stored_asset_is_not_public_until_embed_commits(ready_catalog: ReadyCatalog) -> None:
    assert ready_catalog.ingest.enqueue([HARBOR]) == {}
    ready_catalog.ingest.drain()
    asset_id = ready_catalog.scalar("SELECT id FROM assets")
    assert isinstance(asset_id, UUID)
    with TestClient(create_app(ready_catalog.settings)) as client:
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 404
        assert client.get("/api/v1/assets").json()["items"] == []
    ready_catalog.embed.drain()
    with TestClient(create_app(ready_catalog.settings)) as client:
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 200


def test_worker_cannot_update_an_embedding_row(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR])
    with psycopg.connect(ready_catalog.database.worker_url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                f"UPDATE {catalog.EMBEDDINGS_TABLE} SET model_version = 'other'"
            )


class _NamedCheckpoint:
    """An identity with no weights. enqueue and claim must not encode."""

    def __init__(self, model_key: str, model_version: str) -> None:
        self.model_key = model_key
        self.model_version = model_version
        self.dimensions = 512

    def prepare(self) -> None:
        return None

    def embed_image(self, image_bytes: bytes) -> list[float]:
        raise AssertionError("weights are not loaded")

    def embed_text(self, text: str) -> list[float]:
        raise AssertionError("weights are not loaded")


def _embed_for(
    catalog_under_test: ReadyCatalog, model_key: str, model_version: str
) -> EmbedService:
    return EmbedService(
        settings=catalog_under_test.settings,
        embeddings=_NamedCheckpoint(model_key, model_version),
        object_store=ObjectStore(
            catalog_under_test._s3,
            catalog_under_test.settings.object_store_bucket,
        ),
        catalog_store=catalog_under_test.catalog,
        job_store=catalog_under_test.job_store,
    )


def _reject(catalog_under_test: ReadyCatalog, asset_id: str) -> None:
    source_id = catalog_under_test.scalar(
        "SELECT source_record_id FROM assets WHERE id = :asset_id",
        asset_id=UUID(asset_id),
    )
    assert isinstance(source_id, UUID)
    catalog_under_test.ingestion_repository.record_superseding_rights(
        source_id,
        rights_type=catalog.RIGHTS_REJECTED,
        verification_status=catalog.VERIFICATION_REJECTED,
        verification_method=catalog.METHOD_OPERATOR_REVIEW,
        evidence_url=None,
        raw_statement="rights withdrawn",
        decision_reason="operator_rejection",
    )


def test_a_newer_rejection_unpublishes_the_asset(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])
    with TestClient(create_app(ready_catalog.settings)) as client:
        listed = client.get("/api/v1/assets").json()["items"]
        harbor = next(item for item in listed if item["title"] == "Harbor at Dawn")
        field = next(item for item in listed if item["title"] == "Field in Late Summer")
        assert client.get(f"/api/v1/assets/{harbor['id']}").json()["rights_type"] == "CC0"
        before = client.get(f"/api/v1/assets/{field['id']}/related").json()["items"]
        assert any(item["id"] == harbor["id"] for item in before)

        _reject(ready_catalog, harbor["id"])
        source_id = ready_catalog.scalar(
            "SELECT source_record_id FROM assets WHERE id = :asset_id",
            asset_id=UUID(harbor["id"]),
        )
        assert isinstance(source_id, UUID)
        latest = ready_catalog.ingestion_repository.latest_evidence(source_id)
        assert latest is not None
        assert latest.rights_type == catalog.RIGHTS_REJECTED
        assert ready_catalog.ingestion_repository.evidence_count(source_id) == 2
        assert (
            ready_catalog.scalar(
                "SELECT status FROM assets WHERE id = :asset_id",
                asset_id=UUID(harbor["id"]),
            )
            == catalog.ASSET_SUPPRESSED
        )

        detail = client.get(f"/api/v1/assets/{harbor['id']}")
        assert detail.status_code == 404
        assert "CC0" not in detail.text
        titles = [item["title"] for item in client.get("/api/v1/assets").json()["items"]]
        assert "Harbor at Dawn" not in titles
        ranked = client.get("/api/v1/assets", params={"q": "harbor"}).json()["items"]
        assert all(item["id"] != harbor["id"] for item in ranked)
        related = client.get(f"/api/v1/assets/{field['id']}/related").json()["items"]
        assert all(item["id"] != harbor["id"] for item in related)


def test_fake_vectors_do_not_satisfy_the_openclip_identity(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])
    assert ready_catalog.embeddings.model_key == FAKE_MODEL_KEY
    assert ready_catalog.embeddings.model_version == FAKE_MODEL_VERSION
    assert ready_catalog.settings.model_version == OPENCLIP_VERSION
    assert ready_catalog.scalar(
        f"""
        SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}
        WHERE model_key = :model_key AND model_version = :model_version
        """,
        model_key=FAKE_MODEL_KEY,
        model_version=FAKE_MODEL_VERSION,
    ) == 2
    assert (
        ready_catalog.scalar(
            f"""
            SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}
            WHERE model_key = :model_key AND model_version = :model_version
            """,
            model_key=OPENCLIP_KEY,
            model_version=OPENCLIP_VERSION,
        )
        == 0
    )
    assert ready_catalog.embed.enqueue_missing() == 0

    queued = _embed_for(ready_catalog, OPENCLIP_KEY, OPENCLIP_VERSION).enqueue_missing()
    assert queued == 2
    assert (
        ready_catalog.scalar(
            """
            SELECT count(*) FROM jobs
            WHERE kind = :kind
              AND state = :pending
              AND payload->>'model_key' = :model_key
              AND payload->>'model_version' = :model_version
            """,
            kind=catalog.JOB_KIND_EMBED,
            pending=catalog.JOB_PENDING,
            model_key=OPENCLIP_KEY,
            model_version=OPENCLIP_VERSION,
        )
        == 2
    )
    assert (
        ready_catalog.scalar(f"SELECT count(*) FROM {catalog.EMBEDDINGS_TABLE}") == 2
    )


def test_embed_claim_leaves_another_checkpoint_pending(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR])
    asset_id = ready_catalog.scalar("SELECT id FROM assets")
    assert isinstance(asset_id, UUID)
    dedupe = f"embed:{OPENCLIP_KEY}:{OPENCLIP_VERSION}:{asset_id}"
    assert ready_catalog.job_store.enqueue(
        dedupe,
        {
            "asset_id": str(asset_id),
            "model_key": OPENCLIP_KEY,
            "model_version": OPENCLIP_VERSION,
            "original_key": "originals/unused",
        },
        max_attempts=4,
        kind=catalog.JOB_KIND_EMBED,
    )
    assert ready_catalog.job_store.enqueue(
        "ingest:met:probe",
        {},
        max_attempts=4,
        kind=catalog.JOB_KIND_INGEST,
    )

    outcomes = _embed_for(ready_catalog, OPENCLIP_KEY, "other-checkpoint").drain()
    assert outcomes == []
    assert (
        ready_catalog.scalar(
            "SELECT state FROM jobs WHERE dedupe_key = :dedupe_key",
            dedupe_key=dedupe,
        )
        == catalog.JOB_PENDING
    )
    assert (
        ready_catalog.scalar(
            "SELECT attempt_count FROM jobs WHERE dedupe_key = :dedupe_key",
            dedupe_key=dedupe,
        )
        == 0
    )

    ingest_job = ready_catalog.job_store.claim(lease_seconds=30)
    assert ingest_job is not None
    assert ingest_job.kind == catalog.JOB_KIND_INGEST
    assert ingest_job.dedupe_key == "ingest:met:probe"

    claimed = ready_catalog.job_store.claim(
        lease_seconds=30,
        kind=catalog.JOB_KIND_EMBED,
        model_key=OPENCLIP_KEY,
        model_version=OPENCLIP_VERSION,
    )
    assert claimed is not None
    assert claimed.dedupe_key == dedupe
    assert claimed.attempt_count == 1


def test_ranked_pages_stay_on_the_snapshot_after_suppression(
    ready_catalog: ReadyCatalog,
) -> None:
    _publish(ready_catalog, [HARBOR, FIELD, REEDS])
    query = "qqqq-no-lexeme"
    with TestClient(create_app(ready_catalog.settings)) as client:
        first = client.get("/api/v1/assets", params={"q": query, "limit": 1})
        assert first.status_code == 200
        page1 = first.json()
        assert len(page1["items"]) == 1
        cursor = page1["next_cursor"]
        assert cursor
        page1_id = page1["items"][0]["id"]

        second = client.get(
            "/api/v1/assets", params={"q": query, "limit": 1, "cursor": cursor}
        )
        assert second.status_code == 200
        page2_id = second.json()["items"][0]["id"]
        third_cursor = second.json()["next_cursor"]
        assert third_cursor
        third = client.get(
            "/api/v1/assets", params={"q": query, "limit": 1, "cursor": third_cursor}
        )
        page3_id = third.json()["items"][0]["id"]
        assert len({page1_id, page2_id, page3_id}) == 3

        _reject(ready_catalog, page1_id)
        again = client.get(
            "/api/v1/assets", params={"q": query, "limit": 1, "cursor": cursor}
        )
        assert again.status_code == 200
        assert [item["id"] for item in again.json()["items"]] == [page2_id]

        wrong_query = client.get(
            "/api/v1/assets",
            params={"q": "different-query", "limit": 1, "cursor": cursor},
        )
        assert wrong_query.status_code == 400
        filtered = client.get(
            "/api/v1/assets",
            params={"q": query, "limit": 1, "cursor": cursor, "year_from": 1},
        )
        assert filtered.status_code == 400
        payload = decode_cursor(cursor)
        payload["model_version"] = "other-checkpoint"
        forged = client.get(
            "/api/v1/assets",
            params={"q": query, "limit": 1, "cursor": encode_cursor(payload)},
        )
        assert forged.status_code == 400
