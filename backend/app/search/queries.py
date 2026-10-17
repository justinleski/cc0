"""Pure PostgreSQL query construction for public search.

Builders return SQL and bound parameters without opening connections. Only
trusted catalog constants and internally generated placeholders enter SQL text.
"""

from typing import Any

from app.db import catalog
from app.search.models import BrowsePosition, SearchFilters
from app.search.parse import ParsedQuery
from app.search.rank import CANDIDATE_POOL

_CARD_COLUMNS = """
assets.id, assets.title, assets.creator_id, assets.creator_name,
assets.location_label, assets.created_year_start, assets.created_year_end,
assets.thumbnail_key, assets.ingested_at, assets.width, assets.height
"""

_PUBLIC_FROM = f"FROM {catalog.READY_ASSETS_VIEW} AS assets"


ASSET_DETAIL = f"""
SELECT asset_id, title, creator_id, creator_name, location_label,
       created_year_start, created_year_end, thumbnail_key, ingested_at,
       description, original_key, width, height, landing_url,
       rights_type, evidence_url, verified_at
FROM {catalog.READY_ASSETS_VIEW}
WHERE asset_id = :asset_id
"""

PUBLIC_ASSET_EXISTS = f"""
SELECT 1 FROM {catalog.READY_ASSETS_VIEW} WHERE asset_id = :asset_id
"""

RELATED = f"""
SELECT {_CARD_COLUMNS},
       (
           assets.creator_id IS NOT NULL
           AND assets.creator_id = seed.creator_id
       ) AS same_creator,
       (
           assets.location_label IS NOT NULL
           AND assets.location_label = seed.location_label
       ) AS same_location,
       (
           cardinality(assets.style_tags) > 0
           AND assets.style_tags && seed.style_tags
       ) AS same_style
{_PUBLIC_FROM}
JOIN {catalog.READY_ASSETS_VIEW} AS seed ON seed.id = :asset_id
JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
  ON embedding.asset_id = assets.id
 AND embedding.model_key = :model_key
 AND embedding.model_version = :model_version
JOIN {catalog.EMBEDDINGS_TABLE} AS seed_embedding
  ON seed_embedding.asset_id = seed.id
 AND seed_embedding.model_key = :model_key
 AND seed_embedding.model_version = :model_version
WHERE assets.id <> seed.id
ORDER BY embedding.embedding <=> seed_embedding.embedding, assets.id
LIMIT :limit
"""

CARDS = f"""
SELECT {_CARD_COLUMNS}
{_PUBLIC_FROM}
WHERE assets.id = ANY(:ids)
"""

CREATORS = "SELECT id, name FROM creators ORDER BY name, id LIMIT :limit"

CREATOR_SIMILARITY = """
SELECT id, name
FROM creators
WHERE name % :query
   OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(aliases) AS alias
        WHERE alias % :query
   )
ORDER BY GREATEST(
    similarity(name, :query),
    COALESCE((
        SELECT max(similarity(alias, :query))
        FROM jsonb_array_elements_text(aliases) AS alias
    ), 0)
) DESC, name, id
LIMIT :limit
"""


def browse_query(
    filters: SearchFilters,
    limit: int,
    after: BrowsePosition | None,
) -> tuple[str, dict[str, Any]]:
    where, params = _filters(ParsedQuery("", (), ""), filters)
    statement = f"""
        SELECT {_CARD_COLUMNS}
        {_PUBLIC_FROM}
        WHERE {where}
    """
    query_params = dict(params)
    if after is not None:
        statement += """
          AND (assets.ingested_at, assets.id) < (:cursor_at, :cursor_id)
        """
        query_params["cursor_at"] = after.ingested_at
        query_params["cursor_id"] = after.id
    statement += """
        ORDER BY assets.ingested_at DESC, assets.id DESC
        LIMIT :limit
    """
    query_params["limit"] = limit
    return statement, query_params


def _filters(
    parsed: ParsedQuery,
    filters: SearchFilters,
) -> tuple[str, dict[str, Any]]:
    creator_id = filters.creator_id
    location = filters.location
    year_from = filters.year_from
    year_to = filters.year_to
    # Visibility is the ready_assets view. These clauses are the caller's filters.
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if creator_id is not None:
        clauses.append("assets.creator_id = :creator_id")
        params["creator_id"] = creator_id
    if location:
        clauses.append("assets.location_label = :location")
        params["location"] = location
    if year_from is not None or year_to is not None:
        clauses.append(
            "(assets.created_year_start IS NOT NULL OR assets.created_year_end IS NOT NULL)"
        )
    if year_from is not None:
        clauses.append("COALESCE(assets.created_year_end, assets.created_year_start) >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        clauses.append("COALESCE(assets.created_year_start, assets.created_year_end) <= :year_to")
        params["year_to"] = year_to
    for index, phrase in enumerate(parsed.phrases):
        key = f"phrase_{index}"
        clauses.append(f"assets.lexeme_simple @@ phraseto_tsquery('simple', :{key})")
        params[key] = phrase
    if not clauses:
        return "TRUE", params
    return " AND ".join(clauses), params


def lexical_query(
    parsed: ParsedQuery,
    filters: SearchFilters,
) -> tuple[str, dict[str, Any]] | None:
    where, params = _filters(parsed, filters)
    if parsed.quotes_only:
        rank = " + ".join(
            f"ts_rank_cd(assets.lexeme_simple, phraseto_tsquery('simple', :phrase_{index}))"
            for index in range(len(parsed.phrases))
        )
        statement = f"""
            SELECT assets.id
            {_PUBLIC_FROM}
            WHERE {where}
            ORDER BY {rank} DESC, assets.id
            LIMIT :pool
        """
        return statement, {**params, "pool": CANDIDATE_POOL}
    if not parsed.unquoted:
        return None
    statement = f"""
        SELECT assets.id
        {_PUBLIC_FROM}
        WHERE {where}
          AND (
              assets.lexeme_simple @@ plainto_tsquery('simple', :unquoted)
              OR assets.lexeme_english @@ plainto_tsquery('english', :unquoted)
          )
        ORDER BY
            ts_rank_cd(assets.lexeme_simple, plainto_tsquery('simple', :unquoted))
          + ts_rank_cd(assets.lexeme_english, plainto_tsquery('english', :unquoted))
            DESC,
            assets.id
        LIMIT :pool
    """
    return statement, {**params, "unquoted": parsed.unquoted, "pool": CANDIDATE_POOL}


def semantic_query(
    parsed: ParsedQuery,
    filters: SearchFilters,
    vector: list[float],
    model_key: str,
    model_version: str,
) -> tuple[str, dict[str, Any]]:
    where, params = _filters(parsed, filters)
    statement = f"""
        SELECT assets.id
        {_PUBLIC_FROM}
        JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
          ON embedding.asset_id = assets.id
         AND embedding.model_key = :model_key
         AND embedding.model_version = :model_version
        WHERE {where}
        ORDER BY embedding.embedding <=> CAST(:vector AS vector), assets.id
        LIMIT :pool
    """
    return statement, {
        **params,
        "model_key": model_key,
        "model_version": model_version,
        "vector": _vector_literal(vector),
        "pool": CANDIDATE_POOL,
    }


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(format(value, ".8g") for value in values) + "]"
