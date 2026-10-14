"""The fake provider is exact because the test owns the vectors. Real CLIP is not."""

import math

import pytest

from app.core.config import Settings
from app.embeddings.provider import (
    FAKE_MODEL_KEY,
    FAKE_MODEL_VERSION,
    FakeEmbeddingProvider,
    build_provider,
    cosine,
    normalize_text,
)
from app.worker.jobs import embed_dedupe_key


def test_fake_vectors_are_deterministic_unit_length_and_512():
    provider = FakeEmbeddingProvider(512)
    assert provider.model_key == FAKE_MODEL_KEY
    assert provider.model_version == FAKE_MODEL_VERSION
    image = provider.embed_image(b"same-bytes")
    again = provider.embed_image(b"same-bytes")
    text = provider.embed_text("  Misty   Harbor ")
    folded = provider.embed_text("misty harbor")

    assert image == again
    assert text == folded
    assert len(image) == len(text) == 512
    assert image != text
    assert math.isclose(math.sqrt(sum(value * value for value in image)), 1.0, rel_tol=1e-9)
    assert cosine(image, image) == pytest.approx(1.0)


def test_build_provider_uses_the_fake_without_loading_weights():
    settings = Settings(
        _env_file=None,
        model_key="ViT-B-32",
        model_version="laion2b_s34b_b79k",
    )
    provider = build_provider(settings)
    provider.prepare()
    assert provider.model_key == FAKE_MODEL_KEY
    assert provider.model_version == FAKE_MODEL_VERSION
    assert provider.model_version != settings.model_version
    assert len(provider.embed_text("coast")) == 512


def test_openclip_keeps_the_configured_checkpoint_and_unpinned_refuses_download():
    settings = Settings(
        _env_file=None,
        embedding_provider="openclip",
        model_key="ViT-B-32",
        model_version="laion2b_s34b_b79k",
    )
    provider = build_provider(settings)
    assert (provider.model_key, provider.model_version) == ("ViT-B-32", "laion2b_s34b_b79k")

    unpinned = build_provider(
        Settings(_env_file=None, embedding_provider="openclip", model_version="unpinned")
    )
    with pytest.raises(RuntimeError, match="unpinned"):
        unpinned.prepare()


def test_a_wider_model_is_rejected_before_a_column_could_truncate_it():
    with pytest.raises(ValueError, match="512"):
        Settings(_env_file=None, model_dimensions=768)


def test_an_embedding_setting_typo_is_rejected(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDR", "openclip")
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDR"):
        Settings(_env_file=None)


def test_embed_dedupe_key_includes_the_checkpoint():
    from uuid import UUID

    asset_id = UUID("00000000-0000-0000-0000-000000000001")
    first = embed_dedupe_key("ViT-B-32", "unpinned", asset_id)
    revised = embed_dedupe_key("ViT-B-32", "laion2b_s34b_b79k", asset_id)
    assert first == f"embed:ViT-B-32:unpinned:{asset_id}"
    assert first != revised


def test_normalize_text_casefolds_and_collapses_space():
    assert normalize_text("  DaVinci\n") == "davinci"
