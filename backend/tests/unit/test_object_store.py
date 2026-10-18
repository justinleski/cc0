"""Object-store failures stay inside ObjectStoreUnavailable, including the body read."""

import pytest
from botocore.exceptions import BotoCoreError

from app.integrations.object_store import ObjectStore, ObjectStoreUnavailable


class _Body:
    def __init__(self) -> None:
        self.closed = False

    def read(self) -> bytes:
        raise BotoCoreError()

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.body = _Body()

    def get_object(self, **_kwargs: object) -> dict[str, _Body]:
        return {"Body": self.body}


def test_a_body_read_error_is_unavailable_and_the_body_is_closed():
    client = _Client()
    store = ObjectStore(client, "cc0")
    with pytest.raises(ObjectStoreUnavailable):
        store.get_bytes("originals/ab/cd/dead.jpg")
    assert client.body.closed


class _Upload:
    def __init__(self, length: int) -> None:
        self.length = length
        self.uploaded: list[str] = []

    def upload_fileobj(self, _file: object, _bucket: str, key: str, **_kwargs: object) -> None:
        self.uploaded.append(key)

    def head_object(self, **_kwargs: object) -> dict[str, int]:
        return {"ContentLength": self.length}


def test_put_file_rejects_a_truncated_object_so_it_is_never_published():
    store = ObjectStore(_Upload(length=9), "cc0")
    with pytest.raises(ObjectStoreUnavailable, match="size_mismatch"):
        store.put_file("exports/x.zip", object(), "application/zip", "0" * 64, size=10)
    ObjectStore(_Upload(length=10), "cc0").put_file(
        "exports/x.zip", object(), "application/zip", "0" * 64, size=10
    )
