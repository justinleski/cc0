"""Recorded provider responses, replayed without opening a socket.

Used by the seed CLI and by tests, so what CI exercises is what an operator
runs locally. The same allowlist validation applies here as on the network
path: a recorded fixture cannot smuggle in an unlisted host, and a non-image
body is refused rather than stored.

Every call is appended to `requests`, which is how a test proves that a non-CC0
record never triggered an image GET.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ingestion import met
from app.ingestion.errors import PermanentFailure
from app.ingestion.fetch import (
    FetchedImage,
    Guard,
    classify_status,
    validate_url,
    verify_content_type,
)

CATALOG_FILE = "catalog.json"
OBJECTS_DIR = "objects"
IMAGES_DIR = "images"
IMAGE_MANIFEST = "manifest.json"

METADATA_REQUEST = "metadata"
IMAGE_REQUEST = "image"


@dataclass
class FixtureSource:
    """A JsonSource and an ImageFetcher over one recorded catalog directory."""

    root: Path
    metadata_hosts: frozenset[str]
    image_hosts: frozenset[str]
    requests: list[tuple[str, str]] = field(default_factory=list)

    def logged(self, kind: str) -> list[str]:
        return [url for request_kind, url in self.requests if request_kind == kind]

    @property
    def image_requests(self) -> list[str]:
        return self.logged(IMAGE_REQUEST)

    @property
    def metadata_requests(self) -> list[str]:
        return self.logged(METADATA_REQUEST)

    def get_json(self, url: str) -> Any:
        target = validate_url(url, self.metadata_hosts, what="metadata")
        self.requests.append((METADATA_REQUEST, target))
        if target == met.catalog_url():
            return self._read_json(self.root / CATALOG_FILE)
        prefix = f"{met.BASE_URL}/objects/"
        if target.startswith(prefix):
            external_id = target.removeprefix(prefix)
            return self._read_json(self.root / OBJECTS_DIR / f"{external_id}.json")
        raise PermanentFailure("fixture_missing", "unrecorded metadata url")

    def fetch(self, url: str, *, guard: Guard | None = None) -> FetchedImage:
        target = validate_url(url, self.image_hosts, what="image")
        if guard is not None:
            guard()
        self.requests.append((IMAGE_REQUEST, target))
        entry = self._manifest().get(target)
        if entry is None:
            raise PermanentFailure("fixture_missing", "unrecorded image url")
        # A recorded non-200 keeps the status mapping honest without a server.
        classify_status(int(entry.get("status", 200)), entry.get("headers", {}))
        content_type = verify_content_type(entry.get("content_type"))
        body = (self.root / IMAGES_DIR / entry["file"]).read_bytes()
        return FetchedImage(url=target, content_type=content_type, body=body)

    def _manifest(self) -> dict[str, dict[str, Any]]:
        return self._read_json(self.root / IMAGES_DIR / IMAGE_MANIFEST)

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            # The provider answering 404 for an unknown id is also permanent.
            raise PermanentFailure("fixture_missing", path.name) from exc
        except json.JSONDecodeError as exc:
            raise PermanentFailure("fixture_not_json", path.name) from exc
