"""Embedding service sequences catalog reads, object reads, inference, and publication."""

from types import SimpleNamespace
from uuid import uuid4

from app.core.config import Settings
from app.db import catalog
from app.embeddings.models import Asset
from app.embeddings.service import ALREADY_EMBEDDED, EMBEDDED, EmbedService
from app.worker.jobs import ClaimedJob


def test_new_vector_is_published_after_object_read_and_inference():
    events = []
    asset_id = uuid4()

    class Repository:
        def get_asset_by_id(self, given):
            events.append("asset")
            assert given == asset_id
            return Asset(asset_id, catalog.ASSET_STORED, "hash", "original", "thumb")

        def embedding_exists(self, *_):
            events.append("exists")
            return False

        def save_embedding(self, **kwargs):
            events.append("publish")
            assert kwargs["vector"] == [0.25, 0.75]

    class Objects:
        def get_bytes(self, key):
            events.append("object")
            assert key == "original"
            return b"image"

    class Embeddings:
        model_key = "fake"
        model_version = "v1"
        dimensions = 2

        def embed_image(self, body):
            events.append("inference")
            assert body == b"image"
            return [0.25, 0.75]

    service = EmbedService(
        settings=Settings(), embeddings=Embeddings(), object_store=Objects(),
        repository=Repository(), job_store=SimpleNamespace(),
    )
    job = ClaimedJob(
        uuid4(), catalog.JOB_KIND_EMBED, "embed:fake:v1:asset", 1, 4,
        {"asset_id": str(asset_id), "model_key": "fake", "model_version": "v1",
         "original_key": "original"}, uuid4(),
    )
    assert service._execute(job).result == EMBEDDED
    assert events == ["asset", "exists", "object", "inference", "publish"]


def test_existing_vector_skips_object_read_and_inference():
    events = []
    asset_id = uuid4()

    class Repository:
        def get_asset_by_id(self, _):
            events.append("asset")
            return Asset(asset_id, catalog.ASSET_STORED, "hash", "original", "thumb")

        def embedding_exists(self, *_):
            events.append("exists")
            return True

        def publish_existing_embedding(self, *_):
            events.append("publish_existing")

    class NoRead:
        def get_bytes(self, _):
            raise AssertionError("existing vector must skip object read")

    embeddings = SimpleNamespace(model_key="fake", model_version="v1", dimensions=2)
    service = EmbedService(
        settings=Settings(), embeddings=embeddings, object_store=NoRead(),
        repository=Repository(), job_store=SimpleNamespace(),
    )
    job = ClaimedJob(
        uuid4(), catalog.JOB_KIND_EMBED, "embed:fake:v1:asset", 1, 4,
        {"asset_id": str(asset_id), "model_key": "fake", "model_version": "v1",
         "original_key": "original"}, uuid4(),
    )
    assert service._execute(job).result == ALREADY_EMBEDDED
    assert events == ["asset", "exists", "publish_existing"]
