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
