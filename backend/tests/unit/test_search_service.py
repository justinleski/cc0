"""Search behavior without PostgreSQL, HTTP, or model downloads."""

import base64
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.embeddings.provider import FakeEmbeddingProvider
from app.search.cursor import InvalidCursor, decode_cursor, encode_cursor
from app.search.models import (
    AssetCard,
    BrowsePosition,
    CreatorHit,
    RankedCandidates,
    SearchFilters,
)
from app.search.parse import parse_search_text
from app.search.service import SearchQueryError, SearchService


def card(number: int) -> AssetCard:
    return AssetCard(
        id=UUID(int=number),
        title=str(number),
        creator_id=None,
        creator_name=None,
        location_label=None,
        created_year_start=None,
        created_year_end=None,
        thumbnail_key=f"{number}.webp",
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        width=640,
        height=480,
    )


class MemoryRepository:
    def __init__(self, events: list) -> None:
        self.events = events
        self.visible = {item.id: item for item in [card(i) for i in range(1, 6)]}
        self.ranks = RankedCandidates(list(self.visible), [])
        self.creator_hits = []
        self.detail = None
        self.related_hits = None

    def browse(self, filters, limit, after):
        self.events.append(("browse", filters, limit, after))
        items = sorted(self.visible.values(), key=lambda item: item.id, reverse=True)
        if after:
            items = [item for item in items if item.id < after.id]
        return items[:limit]

    def candidates(self, parsed, filters, vector, model_key, model_version):
        self.events.append(("candidates", parsed, filters, vector, model_key, model_version))
        return self.ranks

    def cards(self, asset_ids):
        self.events.append(("cards", asset_ids))
        return [self.visible[asset_id] for asset_id in asset_ids if asset_id in self.visible]

    def creators(self, hint, limit):
        self.events.append(("creators", hint, limit))
        return self.creator_hits[:limit]

    def asset(self, asset_id):
        self.events.append(("asset", asset_id))
        return self.detail

    def related(self, asset_id, limit, model_key, model_version):
        self.events.append(("related", asset_id, limit, model_key, model_version))
        return self.related_hits


class RecordingEmbeddings(FakeEmbeddingProvider):
    def __init__(self, events: list) -> None:
        super().__init__(dimensions=2)
        self.events = events

    def embed_text(self, text):
        self.events.append(("embed", text))
        return [1.0, 0.0]


@pytest.fixture
def search_context():
    events = []
    repository = MemoryRepository(events)
    embeddings = RecordingEmbeddings(events)
    return SearchService(repository, embeddings, b"test-cursor-key"), repository, embeddings, events


def search(service, **overrides):
    return service.search(
        **{
            "q": None,
            "creator_id": None,
            "location": None,
            "year_from": None,
            "year_to": None,
            "limit": 2,
            "cursor": None,
            **overrides,
        }
    )


def test_browse_uses_keyset_and_lookahead_without_embedding(search_context):
    service, _, _, events = search_context
    first = search(service, q="  ", location="Paris")
    assert first.items == [card(5), card(4)]
    assert first.applied_query == ""
    assert first.suggestion is None
    second = search(service, location="Paris", cursor=first.next_cursor)
    third = search(service, location="Paris", cursor=second.next_cursor)
    assert second.items == [card(3), card(2)]
    assert third.items == [card(1)]
    assert third.next_cursor is None
    assert events == [
        ("browse", SearchFilters(location="Paris"), 3, None),
        (
            "browse",
            SearchFilters(location="Paris"),
            3,
            BrowsePosition(card(4).ingested_at, card(4).id),
        ),
        (
            "browse",
            SearchFilters(location="Paris"),
            3,
            BrowsePosition(card(2).ingested_at, card(2).id),
        ),
    ]


def test_mixed_search_encodes_before_retrieval_and_fuses_ranks(search_context):
    service, repository, embeddings, events = search_context
    repository.ranks = RankedCandidates([card(1).id, card(2).id], [card(2).id, card(3).id])
    page = search(
        service,
        q='"harbor" coastal',
        creator_id=UUID(int=9),
        location="Paris",
        year_from=1800,
        year_to=1900,
        limit=3,
    )
    assert page.items == [card(2), card(1), card(3)]
    assert page.next_cursor is None
    assert events[:2] == [
        ("embed", "coastal"),
        (
            "candidates",
            parse_search_text('"harbor" coastal'),
            SearchFilters(UUID(int=9), "Paris", 1800, 1900),
            [1.0, 0.0],
            embeddings.model_key,
            embeddings.model_version,
        ),
    ]


def test_quotes_only_preserves_lexical_order_without_embedding(search_context):
    service, repository, _, events = search_context
    repository.ranks = RankedCandidates([card(3).id, card(1).id], [])
    page = search(service, q='"harbor"')
    assert page.items == [card(3), card(1)]
    assert events[0][0] == "candidates"
    assert events[0][1].phrases == ("harbor",)
    assert events[0][3] is None
    assert not any(event[0] == "embed" for event in events)


def test_ranked_pages_keep_snapshot_positions_when_cards_are_suppressed(search_context):
    service, repository, _, events = search_context
    first = search(service, q="coastal")
    assert first.items == [card(1), card(2)]
    del repository.visible[card(3).id]
    repository.visible[card(6).id] = card(6)
    repository.ranks = RankedCandidates([card(6).id], [])
    events.clear()
    second = search(service, q="coastal", cursor=first.next_cursor)
    third = search(service, q="coastal", cursor=second.next_cursor)
    assert second.items == [card(4)]
    assert third.items == [card(5)]
    assert third.next_cursor is None
    assert decode_cursor(second.next_cursor)["offset"] == 4
    assert events == [("cards", [card(3).id, card(4).id]), ("cards", [card(5).id])]


def test_ranked_cursor_rejects_forged_snapshot_ids_before_io(search_context):
    service, repository, _, events = search_context
    first = search(service, q='"harbor"', location="Paris", limit=1)
    payload = decode_cursor(first.next_cursor)
    payload["ids"][1] = str(card(5).id)
    events.clear()
    with pytest.raises(InvalidCursor):
        search(service, q='"harbor"', location="Paris", limit=1, cursor=encode_cursor(payload))
    assert events == []


def test_cursor_rejects_oversized_payload_before_io(search_context):
    service, _, _, events = search_context
    with pytest.raises(InvalidCursor):
        search(service, q="coastal", cursor="A" * 22001)
    decoded_oversize = base64.urlsafe_b64encode(b"{" + b" " * 16384 + b"}").decode()
    with pytest.raises(InvalidCursor):
        decode_cursor(decoded_oversize)
    assert events == []


def test_oversized_search_scope_rejected_before_io(search_context):
    service, _, _, events = search_context
    for overrides in ({"q": "x" * 2049}, {"location": "x" * 513}, {"q": "😀" * 513}):
        with pytest.raises(SearchQueryError, match="too long"):
            search(service, **overrides)
    assert events == []


def test_unicode_query_within_scope_limit_can_paginate(search_context):
    service, _, _, _ = search_context
    query = "😀" * 512
    first = search(service, q=query, limit=1)
    assert first.next_cursor
    assert search(service, q=query, limit=1, cursor=first.next_cursor).items == [card(2)]


@pytest.mark.parametrize(
    "changed",
    [
        {"q": "other"},
        {"q": '"coastal"'},
        {"creator_id": UUID(int=9)},
        {"location": "Paris"},
        {"year_from": 1800},
        {"year_to": 1900},
        {"model_key": "other"},
        {"model_version": "v2"},
    ],
)
def test_ranked_cursor_rejects_scope_changes_before_io(search_context, changed):
    service, _, embeddings, events = search_context
    first = search(service, q="coastal")
    overrides = dict(changed)
    for field in ("model_key", "model_version"):
        if field in overrides:
            setattr(embeddings, field, overrides.pop(field))
    events.clear()
    with pytest.raises(InvalidCursor):
        search(service, **{"q": "coastal", "cursor": first.next_cursor, **overrides})
    assert events == []


@pytest.mark.parametrize(
    "query,cursor",
    [
        (None, "invalid"),
        ("coastal", "invalid"),
        (None, encode_cursor({"t": "rank"})),
        ("coastal", encode_cursor({"t": "browse"})),
        (None, encode_cursor({"t": "browse", "at": "bad", "id": "bad"})),
    ],
)
def test_invalid_cursor_rejected_before_io(search_context, query, cursor):
    service, _, _, events = search_context
    with pytest.raises(InvalidCursor):
        search(service, q=query, cursor=cursor)
    assert events == []


def test_invalid_year_range_rejected_before_io(search_context):
    service, _, _, events = search_context
    with pytest.raises(SearchQueryError, match="year_from cannot be after year_to"):
        search(service, q="coastal", year_from=1900, year_to=1800)
    assert events == []


def test_embedding_failure_does_not_start_repository_work(search_context, monkeypatch):
    service, _, embeddings, events = search_context

    def fail(_text):
        raise RuntimeError("encoder unavailable")

    monkeypatch.setattr(embeddings, "embed_text", fail)
    with pytest.raises(RuntimeError, match="encoder unavailable"):
        search(service, q="coastal")
    assert events == []


def test_rank_snapshot_keeps_the_existing_hundred_result_cap(search_context):
    service, repository, _, _ = search_context
    repository.visible = {UUID(int=i): card(i) for i in range(1, 102)}
    repository.ranks = RankedCandidates(list(repository.visible), [])
    page = search(service, q="coastal", limit=50)
    snapshot = decode_cursor(page.next_cursor)
    assert len(snapshot["ids"]) == 100
    final = search(service, q="coastal", limit=50, cursor=page.next_cursor)
    assert final.items == [card(i) for i in range(51, 101)]
    assert final.next_cursor is None


@pytest.mark.parametrize("query", ["Vincent van Goh", '"Vincent van Goh"'])
def test_empty_ranked_search_suggests_a_creator(search_context, query):
    service, repository, _, events = search_context
    repository.ranks = RankedCandidates([], [])
    repository.creator_hits = [CreatorHit(UUID(int=9), "Vincent van Gogh")]
    page = search(service, q=query)
    assert page.items == []
    assert page.next_cursor is None
    assert page.suggestion == "Vincent van Gogh"
    assert events[-1] == ("creators", "Vincent van Goh", 1)


def test_hidden_first_page_does_not_suggest_or_fill_from_later_positions(search_context):
    service, repository, _, events = search_context
    repository.visible.clear()
    page = search(service, q="coastal")
    assert page.items == []
    assert page.next_cursor is not None
    assert page.suggestion is None
    assert not any(event[0] == "creators" for event in events)


@pytest.mark.parametrize(
    "query,expected",
    [
        (None, None),
        ("  ", None),
        ("vincent VAN GOGH", None),
        ("  Vincent   van Goh  ", "Vincent van Gogh"),
    ],
)
def test_creator_suggestions_normalize_input_and_ignore_case(search_context, query, expected):
    service, repository, _, events = search_context
    repository.creator_hits = [CreatorHit(UUID(int=9), "Vincent van Gogh")]
    page = service.creators(query, 5)
    assert page.creators == repository.creator_hits
    assert page.did_you_mean == expected
    assert events == [("creators", " ".join((query or "").split()), 5)]


def test_empty_search_does_not_suggest_same_name_ignoring_case(search_context):
    service, repository, _, _ = search_context
    repository.ranks = RankedCandidates([], [])
    repository.creator_hits = [CreatorHit(UUID(int=9), "Vincent van Gogh")]
    assert search(service, q="vincent van gogh").suggestion is None


def test_detail_and_related_preserve_missing_versus_empty_results(search_context):
    service, repository, embeddings, events = search_context
    asset_id = card(1).id
    assert service.asset(asset_id) is None
    assert service.related(asset_id, 12) is None
    repository.related_hits = []
    assert service.related(asset_id, 12) == []
    assert events[-1] == (
        "related",
        asset_id,
        12,
        embeddings.model_key,
        embeddings.model_version,
    )
