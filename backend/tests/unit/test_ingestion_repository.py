from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import catalog
from app.ingestion import queries
from app.ingestion.models import SourceRecord
from app.ingestion.repository import IngestionRepository
from app.worker.jobs import ClaimedJob, ClaimLost


def claimed_job():
    return ClaimedJob(uuid4(), "ingest", "ingest:met:1", 1, 4, {}, uuid4())


def asset_arguments(job):
    return dict(
        job=job,
        source_record_id=uuid4(),
        provider_id=uuid4(),
        creator_name="Artist",
        creator_external_key="artist:1",
        creator_external_ids={},
        title="Title",
        description=None,
        location_label=None,
        created_year_start=None,
        created_year_end=None,
        original_key="originals/a",
        thumbnail_key="thumbnails/a",
        sha256="a" * 64,
        mime_type="image/jpeg",
        width=1,
        height=1,
        model_key="fake",
        model_version="deterministic-v1",
        embed_max_attempts=4,
    )


class RecordingEngine:
    def __init__(self, *, fail=None, duplicate=False, claim_held=True, row=None):
        self.fail = fail
        self.duplicate = duplicate
        self.claim_held = claim_held
        self.row = row
        self.active = False
        self.events = []
        self.executed = []
        self.asset_id = uuid4()

    @contextmanager
    def begin(self):
        assert not self.active, "Atomic operations must not open nested connections"
        self.active = True
        self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.active = False
            self.events.append("release")

    def execute(self, statement, params):
        assert self.active
        sql = str(statement)
        self.executed.append((sql, params))
        if sql == self.fail:
            raise RuntimeError("write failed")
        value = self.asset_id
        if "FOR UPDATE" in sql and not self.claim_held:
            value = None
        if sql == queries.INSERT_ASSET and self.duplicate:
            value = None
        return SimpleNamespace(
            scalar_one=lambda: value,
            scalar_one_or_none=lambda: value,
            one_or_none=lambda: self.row,
            one=lambda: self.row,
        )


@pytest.mark.parametrize(
    "fail",
    [
        None,
        queries.UPSERT_CREATOR,
        queries.INSERT_ASSET,
        queries.ENQUEUE_EMBED,
        queries.SUCCEED_INGEST,
    ],
)
def test_asset_and_embed_enqueue_and_job_success_share_transaction(fail):
    engine = RecordingEngine(fail=fail)
    repository = IngestionRepository(engine)
    assert engine.events == []
    job = claimed_job()
    if fail:
        with pytest.raises(RuntimeError, match="write failed"):
            repository.store_asset(**asset_arguments(job))
    else:
        assert repository.store_asset(**asset_arguments(job)) == engine.asset_id
    assert engine.events == ["begin", "rollback" if fail else "commit", "release"]
    assert not engine.active
    assert "FOR UPDATE" in engine.executed[0][0]
    assert engine.executed[0][1]["claim_token"] == job.claim_token
    expected = [
        queries.UPSERT_CREATOR,
        queries.INSERT_ASSET,
        queries.ENQUEUE_EMBED,
        queries.SUCCEED_INGEST,
    ]
    if fail:
        expected = expected[: expected.index(fail) + 1]
    assert [sql for sql, _ in engine.executed[1:]] == expected


def test_duplicate_asset_settles_ingest_without_enqueueing_another_embedding():
    engine = RecordingEngine(duplicate=True)
    assert IngestionRepository(engine).store_asset(**asset_arguments(claimed_job())) is None
    assert queries.ENQUEUE_EMBED not in [sql for sql, _ in engine.executed]
    assert engine.executed[-1][0] == queries.SUCCEED_INGEST
    assert engine.events == ["begin", "commit", "release"]


def test_lost_claim_prevents_every_catalog_write():
    engine = RecordingEngine(claim_held=False)
    with pytest.raises(ClaimLost):
        IngestionRepository(engine).store_asset(**asset_arguments(claimed_job()))
    assert len(engine.executed) == 1
    assert engine.events == ["begin", "rollback", "release"]


@pytest.mark.parametrize(
    "fail", [None, queries.UPDATE_RIGHTS, queries.APPEND_EVIDENCE, queries.SUPPRESS_ASSET]
)
def test_rights_update_evidence_and_suppression_share_transaction(fail):
    engine = RecordingEngine(fail=fail)
    record = SourceRecord(uuid4(), uuid4(), "1", None, None, {}, "", "", None, False)

    def write():
        IngestionRepository(engine).record_rights_decision(
            record,
            job=claimed_job(),
            classification="NON_CC0",
            eligibility=catalog.ELIGIBILITY_NON_CC0_CACHED,
            verification_status="rejected",
            verification_method="test",
            evidence_url=None,
            raw_statement="copyright",
            decision_reason="restricted",
            landing_url=None,
            original_url=None,
            raw_metadata={},
            recheck_days=30,
        )

    if fail:
        with pytest.raises(RuntimeError, match="write failed"):
            write()
    else:
        write()
    assert engine.events == ["begin", "rollback" if fail else "commit", "release"]
    assert "FOR UPDATE" in engine.executed[0][0]
    assert engine.executed[-1][0] == (fail or queries.SUPPRESS_ASSET)


def test_read_maps_driver_row_to_model_and_releases_connection():
    row = SimpleNamespace(
        id=uuid4(),
        provider_id=uuid4(),
        external_id="1",
        landing_url=None,
        original_url=None,
        raw_metadata=None,
        rights_classification="UNKNOWN",
        eligibility_status=catalog.ELIGIBILITY_UNKNOWN_CACHED,
        next_recheck_at=None,
        recheck_due=False,
    )
    engine = RecordingEngine(row=row)
    result = IngestionRepository(engine).get_source_record(row.provider_id, "1")
    assert isinstance(result, SourceRecord)
    assert result.id == row.id and result.raw_metadata == {}
    assert result.is_fresh_negative_cache
    assert engine.events == ["begin", "commit", "release"]
