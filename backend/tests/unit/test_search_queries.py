"""Query construction preserves public visibility and binds untrusted values."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text

from app.db import catalog
from app.search import queries
from app.search.models import BrowsePosition, SearchFilters
from app.search.parse import parse_search_text
from app.search.rank import CANDIDATE_POOL


def compact(statement):
    return " ".join(statement.split())


def test_browse_preserves_keyset_order_and_binds_filters():
    location = "Paris'; DROP TABLE assets; --"
    after = BrowsePosition(datetime(2026, 1, 1, tzinfo=UTC), UUID(int=1))
    filters = SearchFilters(UUID(int=2), location, 1800, 1900)
    statement, params = queries.browse_query(filters, 21, after)
    sql = compact(statement)
    assert location not in sql
    assert f"FROM {catalog.READY_ASSETS_VIEW} AS assets" in sql
    assert "(assets.ingested_at, assets.id) < (:cursor_at, :cursor_id)" in sql
    assert "ORDER BY assets.ingested_at DESC, assets.id DESC LIMIT :limit" in sql
    assert "assets.creator_id = :creator_id" in sql
    assert "assets.location_label = :location" in sql
    assert params == {
        "creator_id": UUID(int=2),
        "location": location,
        "year_from": 1800,
        "year_to": 1900,
        "cursor_at": after.ingested_at,
        "cursor_id": after.id,
        "limit": 21,
    }
    # Every supplied value is bound by SQLAlchemy, including malicious-looking text.
    assert text(statement).bindparams(**params).compile().params == params


def test_unfiltered_browse_has_no_accidental_cursor_or_year_restriction():
    statement, params = queries.browse_query(SearchFilters(), 21, None)
    assert "WHERE TRUE" in compact(statement)
    assert ":cursor_at" not in statement
    assert params == {"limit": 21}


@pytest.mark.parametrize(
    "filters,bound",
    [
        (SearchFilters(year_from=1800), "year_from"),
        (SearchFilters(year_to=1900), "year_to"),
    ],
)
def test_one_sided_year_ranges_exclude_unknown_dates(filters, bound):
    statement, params = queries.browse_query(filters, 21, None)
    assert (
        "assets.created_year_start IS NOT NULL OR assets.created_year_end IS NOT NULL" in statement
    )
    assert bound in params
    if bound == "year_from":
        assert (
            "COALESCE(assets.created_year_end, assets.created_year_start) >= :year_from"
            in statement
        )
        assert "year_to" not in params
    else:
        assert (
            "COALESCE(assets.created_year_start, assets.created_year_end) <= :year_to" in statement
        )
        assert "year_from" not in params


@pytest.mark.parametrize("semantic", [False, True])
def test_mixed_query_requires_each_phrase_and_binds_all_user_values(semantic):
    parsed = parse_search_text('"harbor\'; --" "da vinci" coastal')
    filters = SearchFilters(location="France'; --")
    if semantic:
        statement, params = queries.semantic_query(parsed, filters, [1.0, 0.0], "clip", "v1")
    else:
        statement, params = queries.lexical_query(parsed, filters)
    sql = compact(statement)
    where = sql.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
    for index, phrase in enumerate(parsed.phrases):
        assert f"assets.lexeme_simple @@ phraseto_tsquery('simple', :phrase_{index})" in where
        assert params[f"phrase_{index}"] == phrase
        assert phrase not in sql
    assert filters.location not in sql
    assert "coastal" not in sql
    assert params["pool"] == CANDIDATE_POOL
    assert f"FROM {catalog.READY_ASSETS_VIEW} AS assets" in sql
    assert text(statement).bindparams(**params).compile().params == params
    if semantic:
        assert "embedding.model_key = :model_key" in sql
        assert "embedding.model_version = :model_version" in sql
        assert "ORDER BY embedding.embedding <=> CAST(:vector AS vector), assets.id" in sql
        assert params["vector"] == "[1,0]"
        assert (params["model_key"], params["model_version"]) == ("clip", "v1")
    else:
        assert "ts_rank_cd(assets.lexeme_simple, plainto_tsquery('simple', :unquoted))" in sql
        assert "ts_rank_cd(assets.lexeme_english, plainto_tsquery('english', :unquoted))" in sql
        assert "DESC, assets.id LIMIT :pool" in sql
        assert params["unquoted"] == "coastal"


def test_quotes_only_uses_unstemmed_phrase_ranking():
    statement, params = queries.lexical_query(parse_search_text('"Running"'), SearchFilters())
    sql = compact(statement)
    assert "ORDER BY ts_rank_cd(assets.lexeme_simple, phraseto_tsquery('simple', :phrase_0))" in sql
    assert "DESC, assets.id LIMIT :pool" in sql
    assert "english" not in sql
    assert params == {"phrase_0": "Running", "pool": CANDIDATE_POOL}
    assert queries.lexical_query(parse_search_text(""), SearchFilters()) is None


def test_detail_hydration_and_related_use_public_view_including_the_seed():
    for statement in (
        queries.ASSET_DETAIL,
        queries.CARDS,
        queries.PUBLIC_ASSET_EXISTS,
        queries.RELATED,
    ):
        assert f"FROM {catalog.READY_ASSETS_VIEW}" in statement
    assert f"JOIN {catalog.READY_ASSETS_VIEW} AS seed" in queries.RELATED
    assert "seed_embedding.model_key = :model_key" in queries.RELATED
    assert "seed_embedding.model_version = :model_version" in queries.RELATED
    assert "WHERE assets.id <> seed.id" in queries.RELATED
    assert "ORDER BY embedding.embedding <=> seed_embedding.embedding, assets.id" in queries.RELATED
