"""SQL for board recommendations. Only catalog constants enter SQL text.

Every query reads assets through `ready_assets`, so a pin or candidate whose
latest rights evidence is not verified CC0 is never part of a profile or a
result. Vectors come back as pgvector text, which is a JSON array.
"""

from app.db import catalog

# Owner-scoped like boards' ALL_ITEMS: one row per saved pin, eligible or not.
# A NULL embedding means the pin is ineligible or has no active-model vector.
# The board row is read in the same statement, so the version, the exclusion
# list, and the profile come from one snapshot.
BOARD_PINS = f"""
SELECT board.updated_at, item.asset_id, item.position,
       (asset.id IS NOT NULL) AS eligible,
       asset.title, asset.creator_id, asset.style_tags,
       embedding.embedding::text AS embedding
FROM boards board
LEFT JOIN board_items item ON item.board_id = board.id
LEFT JOIN {catalog.READY_ASSETS_VIEW} AS asset ON asset.id = item.asset_id
LEFT JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
  ON embedding.asset_id = asset.id
 AND embedding.model_key = :model_key
 AND embedding.model_version = :model_version
WHERE board.id = :board_id AND board.owner_user_id = :owner_id
ORDER BY item.position, item.asset_id
"""

# Exact nearest neighbors per probe. `:probes` and `:limits` are parallel
# arrays; each probe gets its own budget. Stable order: distance, then id.
# Both CTEs are MATERIALIZED on purpose:
# - `probe`: inlined, Postgres re-parses the probe's text literal into a vector
#   once per candidate row, which dominated the scan.
# - `eligible`: the rights view runs once per statement, not once per probe.
# The scan selects only ids and distances; card columns and vector text are read
# for the k winners only, because the select list is projected before the sort.
CANDIDATES = f"""
WITH probe AS MATERIALIZED (
    SELECT (raw.ordinality - 1)::integer AS probe_index,
           CAST(raw.literal AS vector) AS vector,
           raw.budget
    FROM unnest(CAST(:probes AS text[]), CAST(:limits AS integer[]))
         WITH ORDINALITY AS raw(literal, budget, ordinality)
),
eligible AS MATERIALIZED (
    SELECT candidate.id, vectors.embedding
    FROM {catalog.READY_ASSETS_VIEW} AS candidate
    JOIN {catalog.EMBEDDINGS_TABLE} AS vectors
      ON vectors.asset_id = candidate.id
     AND vectors.model_key = :model_key
     AND vectors.model_version = :model_version
    WHERE candidate.id <> ALL(CAST(:exclude_ids AS uuid[]))
)
SELECT probe.probe_index, nearest.distance,
       assets.id, assets.title, assets.creator_id, assets.creator_name,
       assets.location_label, assets.created_year_start, assets.created_year_end,
       assets.thumbnail_key, assets.ingested_at, assets.width, assets.height,
       assets.style_tags, nearest.embedding::text AS embedding
FROM probe
CROSS JOIN LATERAL (
    SELECT eligible.id, eligible.embedding, eligible.embedding <=> probe.vector AS distance
    FROM eligible
    ORDER BY distance, eligible.id
    LIMIT probe.budget
) AS nearest
JOIN {catalog.READY_ASSETS_VIEW} AS assets ON assets.id = nearest.id
ORDER BY probe.probe_index, nearest.distance, nearest.id
"""


def vector_literal(values) -> str:
    return "[" + ",".join(format(float(value), ".8g") for value in values) + "]"

# Evaluation only: benchmark boards are files, not owner-scoped board rows.
# Order follows the caller's list; missing ids are ineligible or unembedded.
PINS_BY_ID = f"""
SELECT asset.id AS asset_id, asset.title, asset.creator_id, asset.style_tags,
       embedding.embedding::text AS embedding
FROM {catalog.READY_ASSETS_VIEW} AS asset
JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
  ON embedding.asset_id = asset.id
 AND embedding.model_key = :model_key
 AND embedding.model_version = :model_version
WHERE asset.id = ANY(CAST(:asset_ids AS uuid[]))
"""

# The evaluation corpus: every public asset with an active-model vector.
CORPUS_IDS = f"""
SELECT asset.id
FROM {catalog.READY_ASSETS_VIEW} AS asset
JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
  ON embedding.asset_id = asset.id
 AND embedding.model_key = :model_key
 AND embedding.model_version = :model_version
ORDER BY asset.id
"""
