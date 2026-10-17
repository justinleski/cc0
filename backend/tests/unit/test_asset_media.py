"""Image bytes are gated by current public visibility, including after storage I/O."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.object_store import ObjectStoreUnavailable
from app.main import create_app
from app.search.service import AssetMediaService


@pytest.mark.parametrize("variant,key,mime", [
    ("thumbnail", "thumb.webp", "image/webp"),
    ("original", "original.jpg", "image/jpeg"),
])
def test_media_uses_catalog_keys_and_rechecks_visibility(variant, key, mime):
    detail = SimpleNamespace(card=SimpleNamespace(thumbnail_key="thumb.webp"),
                             original_key="original.jpg")
    repository = Mock()
    repository.asset.return_value = detail
    objects = Mock()
    objects.get_bytes.return_value = b"image"
    asset_id = uuid4()
    image = AssetMediaService(repository, objects).image(asset_id, variant)
    assert image.body == b"image"
    assert image.media_type == mime
    objects.get_bytes.assert_called_once_with(key)
    assert repository.asset.call_count == 2


@pytest.mark.parametrize("visible_before,visible_after,body", [
    (False, False, b"image"), (True, False, b"image"), (True, True, None),
])
def test_hidden_revoked_or_missing_image_fails_closed(visible_before, visible_after, body):
    detail = SimpleNamespace(card=SimpleNamespace(thumbnail_key="thumb.webp"))
    repository = Mock()
    repository.asset.side_effect = [detail if visible_before else None,
                                    detail if visible_after else None]
    objects = Mock()
    objects.get_bytes.return_value = body
    assert AssetMediaService(repository, objects).image(uuid4(), "thumbnail") is None
    assert objects.get_bytes.call_count == int(visible_before)


def test_image_route_hides_storage_errors_and_disables_caching(monkeypatch):
    def unavailable(*args):
        raise ObjectStoreUnavailable("private endpoint and credentials")

    monkeypatch.setattr(AssetMediaService, "image", unavailable)
    app = create_app(Settings(_env_file=None, cursor_signing_key="0123456789abcdef" * 4))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/assets/{uuid4()}/image/original")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "image temporarily unavailable"}
