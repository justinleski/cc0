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
METADATA_FIELDS: tuple[tuple[str, type], ...] = (
    ("objectID", int),
    ("title", str),
    ("artistDisplayName", str),
    ("artistULAN_URL", str),
    ("artistWikidata_URL", str),
    ("objectDate", str),
    ("objectBeginDate", int),
    ("objectEndDate", int),
    ("objectURL", str),
    ("primaryImage", str),
    ("isPublicDomain", bool),
    ("rightsAndReproduction", str),
    ("medium", str),
    ("classification", str),
    ("city", str),
    ("country", str),
)
METADATA_FIELD_NAMES = tuple(name for name, _ in METADATA_FIELDS)

# A data URI anywhere in a value is image bytes wearing a string, so the field
# is dropped rather than stored. The trailing comma is what separates a data
# URI's header from its payload; ordinary prose containing "data:" has none.
_DATA_URI = re.compile(r"data:[^,\s]*,", re.IGNORECASE)
# Generous next to real Met values, and small enough that a hostile or broken
# payload cannot turn the negative cache into bulk storage.
MAX_TEXT_LENGTH = 4096
# Postgres int4, which is what the asset year columns are.
_MIN_YEAR = -2_147_483_648
_MAX_YEAR = 2_147_483_647


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


def _allowlisted(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Keep the allowlisted fields, and only at their expected scalar type.

    Returns the kept fields and the first field whose value was not the shape
    this adapter validates against. A name on the allowlist is not enough: a
    nested object or an oversized string under an allowed name would otherwise
    carry an embedded preview or an unbounded body straight into the negative
    cache, which is supposed to hold metadata and nothing else.

    Every field is still examined after a bad one, so what the cache keeps does
    not depend on which field happened to be wrong first.
    """
    kept: dict[str, Any] = {}
    wrong_shape: str | None = None
    for name, expected in METADATA_FIELDS:
        value = payload.get(name)
        if value is None or value == "":
            continue
        if not _is_expected(value, expected) or (
            isinstance(value, str) and len(value) > MAX_TEXT_LENGTH
        ):
            wrong_shape = wrong_shape or name
            continue
        if isinstance(value, str) and _DATA_URI.search(value):
            continue
        kept[name] = value
    return kept, wrong_shape


def _is_expected(value: Any, expected: type) -> bool:
    # bool is an int in Python, and neither field tolerates the other's values.
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value.strip() or None if isinstance(value, str) else None


def _year(payload: dict[str, Any], name: str) -> int | None:
    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _years_usable(start: int | None, end: int | None) -> bool:
    """Ordered and inside int4, which is what the asset columns accept."""
    for year in (start, end):
        if year is not None and not _MIN_YEAR <= year <= _MAX_YEAR:
            return False
    return start is None or end is None or start <= end


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

    kept, wrong_shape = _allowlisted(payload)
    landing_url = _landing_url(kept)

    def rejected(reason: str) -> MetItem:
        # A rejected payload still earns its item page. That link is what the
        # negative cache is for, and what a recheck or an operator starts from.
        return MetItem(
            external_id=external_id,
            raw_metadata=kept,
            landing_url=landing_url,
            invalid_reason=reason,
        )

    if wrong_shape == "primaryImage":
        # The one field that becomes a URL we would otherwise request.
        return rejected("image_url_not_a_string")
    if wrong_shape is not None:
        return rejected("metadata_field_wrong_type")

    object_id = kept.get("objectID")
    if not isinstance(object_id, int) or isinstance(object_id, bool):
        return rejected("missing_object_id")
    if str(object_id) != str(external_id):
        # A payload for a different object cannot be trusted as evidence about
        # this one, whatever its rights fields say.
        return rejected("object_id_mismatch")

    if landing_url is None:
        return rejected("missing_landing_url")

    year_start = _year(kept, "objectBeginDate")
    year_end = _year(kept, "objectEndDate")
    if not _years_usable(year_start, year_end):
        # Caught here rather than at the asset insert, which happens after both
        # objects are written: a record we cannot describe should never have
        # cost a download.
        return rejected("invalid_year_range")

    original = kept.get("primaryImage")
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
        created_year_start=year_start,
        created_year_end=year_end,
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
    # The policy above states what `isPublicDomain: true` means for the hosted
    # image, which is what lets the rights gate read that flag as a CC0 claim.
    # A source without such a policy must leave this False and fail closed.
    trusted_public_domain_flag = True

    def __init__(self, source: JsonSource) -> None:
        self._source = source

    def discover(self, limit: int) -> Sequence[str]:
        return parse_catalog(self._source.get_json(catalog_url()), limit)

    def fetch_item(self, external_id: str) -> MetItem:
        return normalize(external_id, self._source.get_json(object_url(external_id)))
