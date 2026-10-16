"""Connection and transaction boundaries for embedding persistence."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.embeddings import queries
from app.embeddings.repository import EmbeddingRepository
from app.worker.jobs import ClaimedJob, ClaimLost


class RecordingEngine:
    def __init__(self, fail=None, held=True):
        self.fail = fail
        self.held = held
        self.events = []
        self.active = False

    @contextmanager
    def begin(self):
        assert not self.active
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
        self.events.append(sql)
        if sql == self.fail:
            raise RuntimeError("write failed")
        value = None if "FOR UPDATE" in sql and not self.held else uuid4()
        return SimpleNamespace(scalar_one_or_none=lambda: value)


def test_embedding_query_binds_identity_and_vector():
    asset_id = uuid4()
    params = queries.embedding_params(asset_id, "fake", "v1", 2, [0.25, 0.75])
    assert params == {
        "asset_id": asset_id, "model_key": "fake", "model_version": "v1",
        "dimensions": 2, "embedding": "[0.25,0.75]",
    }
    assert ":embedding" in queries.INSERT_EMBEDDING
    assert "0.25" not in queries.INSERT_EMBEDDING


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fail_at", [None, "publish", "settle"])
def test_publication_is_one_claim_fenced_transaction(existing, fail_at):
    fail = {None: None, "publish": queries.PUBLISH_ASSET, "settle": queries.SUCCEED_JOB}[fail_at]
    engine = RecordingEngine(fail=fail)
    repository = EmbeddingRepository(engine)
    assert engine.events == []
    asset_id = uuid4()
    job = ClaimedJob(uuid4(), "embed", "embed:fake:v1:asset", 1, 4, {}, uuid4())

    def publish():
        if existing:
            repository.publish_existing_embedding(job, asset_id)
        else:
            repository.save_embedding(
                job=job, asset_id=asset_id, model_key="fake", model_version="v1",
                dimensions=2, vector=[0.25, 0.75],
            )

    if fail:
        with pytest.raises(RuntimeError, match="write failed"):
            publish()
    else:
        publish()
    assert engine.events[0] == "begin"
    assert "FOR UPDATE" in engine.events[1]
    writes = engine.events[2:-2]
    expected = ([queries.INSERT_EMBEDDING] if not existing else []) + [
        queries.PUBLISH_ASSET, queries.SUCCEED_JOB
    ]
    assert writes == expected[: len(writes)]
    assert engine.events[-2:] == ["rollback" if fail else "commit", "release"]
    assert not engine.active


@pytest.mark.parametrize("existing", [False, True])
def test_lost_claim_prevents_all_publication_writes(existing):
    engine = RecordingEngine(held=False)
    repository = EmbeddingRepository(engine)
    job = ClaimedJob(uuid4(), "embed", "embed:fake:v1:asset", 1, 4, {}, uuid4())
    with pytest.raises(ClaimLost):
        if existing:
            repository.publish_existing_embedding(job, uuid4())
        else:
            repository.save_embedding(
                job=job, asset_id=uuid4(), model_key="fake", model_version="v1",
                dimensions=2, vector=[0.25, 0.75],
            )
    assert engine.events[0] == "begin"
    assert "FOR UPDATE" in engine.events[1]
    assert engine.events[2:] == ["rollback", "release"]


def test_failed_catalog_read_releases_connection():
    engine = RecordingEngine(fail=queries.ASSET_BY_ID)
    with pytest.raises(RuntimeError, match="write failed"):
        EmbeddingRepository(engine).get_asset_by_id(uuid4())
    assert engine.events == ["begin", queries.ASSET_BY_ID, "rollback", "release"]
    assert not engine.active
