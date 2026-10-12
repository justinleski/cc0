"""The Met Open Access adapter: discovery, metadata allowlist, normalization.

One provider, chosen because its published Open Access policy states what the
item-level `isPublicDomain` flag means for the hosted image. That pairing is the
only passing rights claim in this product; see rights.py.

Nothing here fetches image bytes. Discovery returns external ids, and item
metadata is reduced to the allowlisted fields below before it is stored.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

PROVIDER_NAME = "met"
BASE_URL = "https://collectionapi.metmuseum.org/public/collection/v1"
# The policy that makes the item flag mean CC0. Recorded as evidence on every
# decision. An operator must re-verify this URL and its text before trusting a
# large ingestion run; provider terms change.
POLICY_URL = "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access"
LANDING_HOSTS = frozenset({"www.metmuseum.org", "metmuseum.org"})

# Everything the catalog is allowed to keep from a provider payload. The rest of
# the response is dropped, including any base64 or data-URI preview a provider
# might embed: a negative-cache row must hold metadata, never image bytes.
METADATA_FIELDS = (
    "objectID",
    "title",
    "artistDisplayName",
    "artistULAN_URL",
    "artistWikidata_URL",
    "objectDate",
    "objectBeginDate",
    "objectEndDate",
    "objectURL",
    "primaryImage",
    "isPublicDomain",
    "rightsAndReproduction",
    "medium",
    "classification",
    "city",
    "country",
)

_DATA_URI = re.compile(r"^\s*data:", re.IGNORECASE)


class JsonSource(Protocol):
    """Recorded fixtures or a live HTTP client. Both obey the host allowlist."""

    def get_json(self, url: str) -> Any: ...


@dataclass(frozen=True)
class MetItem:
    """A normalized source record, before any rights decision."""

    external_id: str
    raw_metadata: dict[str, Any]
    landing_url: str | None = None
    original_url: str | None = None
    title: str | None = None
    description: str | None = None
    location_label: str | None = None
    created_year_start: int | None = None
    created_year_end: int | None = None
    creator_name: str | None = None
    creator_external_key: str | None = None
    creator_external_ids: dict[str, str] = field(default_factory=dict)
    is_public_domain: bool | None = None
    rights_statement: str = ""
    # Set when the payload cannot be trusted. The rights gate turns this into
    # UNKNOWN_CACHED rather than a job failure: we learned something about the
    # record, so it earns a recheck date instead of a retry.
    invalid_reason: str | None = None


def catalog_url() -> str:
    return f"{BASE_URL}/objects"


def object_url(external_id: str) -> str:
    return f"{BASE_URL}/objects/{external_id}"


def parse_catalog(payload: Any, limit: int) -> list[str]:
    """External ids from a `/objects` response, in payload order."""
    ids = payload.get("objectIDs") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return []
    external_ids = [str(value) for value in ids if isinstance(value, (int, str)) and str(value)]
    return external_ids[:limit]


def _allowlisted(payload: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for name in METADATA_FIELDS:
        value = payload.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, str) and _DATA_URI.match(value):
            continue
        kept[name] = value
    return kept


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value.strip() or None if isinstance(value, str) else None


def _year(payload: dict[str, Any], name: str) -> int | None:
    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _landing_url(payload: dict[str, Any]) -> str | None:
    url = _text(payload, "objectURL")
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in LANDING_HOSTS:
        return None
    return url


def _location_label(payload: dict[str, Any]) -> str | None:
    parts = [_text(payload, "city"), _text(payload, "country")]
    return ", ".join(part for part in parts if part) or None


def _creator(payload: dict[str, Any]) -> tuple[str | None, str | None, dict[str, str]]:
    """Provider-scoped creator identity.

    An authority URL is a stable key. A display name alone is only a key within
    this provider: merging people across providers or spellings is not a
    rights-safe operation and is not attempted here.
    """
    name = _text(payload, "artistDisplayName")
    external_ids = {
        source: value
        for source, field_name in (("ulan", "artistULAN_URL"), ("wikidata", "artistWikidata_URL"))
        if (value := _text(payload, field_name))
    }
    if not name and not external_ids:
        return None, None, {}
    if "ulan" in external_ids:
        key = f"ulan:{external_ids['ulan']}"
    elif "wikidata" in external_ids:
        key = f"wikidata:{external_ids['wikidata']}"
    else:
        key = f"name:{name.casefold()}"
    return name, key, external_ids


def normalize(external_id: str, payload: Any) -> MetItem:
    """Reduce a provider payload to allowlisted, validated fields."""
    if not isinstance(payload, dict):
        return MetItem(external_id=external_id, raw_metadata={}, invalid_reason="payload_not_json")

    kept = _allowlisted(payload)

    def rejected(reason: str) -> MetItem:
        return MetItem(external_id=external_id, raw_metadata=kept, invalid_reason=reason)

    object_id = kept.get("objectID")
    if not isinstance(object_id, int) or isinstance(object_id, bool):
        return rejected("missing_object_id")
    if str(object_id) != str(external_id):
        # A payload for a different object cannot be trusted as evidence about
        # this one, whatever its rights fields say.
        return rejected("object_id_mismatch")

    landing_url = _landing_url(kept)
    if landing_url is None:
        return rejected("missing_landing_url")

    original = kept.get("primaryImage")
    if original is not None and not isinstance(original, str):
        return rejected("image_url_not_a_string")

    flag = kept.get("isPublicDomain")
    name, creator_key, creator_ids = _creator(kept)
    return MetItem(
        external_id=external_id,
        raw_metadata=kept,
        landing_url=landing_url,
        original_url=original or None,
        title=_text(kept, "title"),
        description=_text(kept, "medium"),
        location_label=_location_label(kept),
        created_year_start=_year(kept, "objectBeginDate"),
        created_year_end=_year(kept, "objectEndDate"),
        creator_name=name,
        creator_external_key=creator_key,
        creator_external_ids=creator_ids,
        is_public_domain=flag if isinstance(flag, bool) else None,
        rights_statement=_text(kept, "rightsAndReproduction") or "",
    )


class MetAdapter:
    """Discovery and item metadata. Both calls are JSON only."""

    provider_name = PROVIDER_NAME
    base_url = BASE_URL
    policy_url = POLICY_URL

    def __init__(self, source: JsonSource) -> None:
        self._source = source

    def discover(self, limit: int) -> Sequence[str]:
        return parse_catalog(self._source.get_json(catalog_url()), limit)

    def fetch_item(self, external_id: str) -> MetItem:
        return normalize(external_id, self._source.get_json(object_url(external_id)))
