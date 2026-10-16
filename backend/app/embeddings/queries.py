"""Embedding catalog SQL and bound parameters; no database I/O."""

from uuid import UUID

from app.db import catalog

ASSET_BY_ID = """
SELECT id, status, sha256, original_key, thumbnail_key FROM assets
WHERE id = :asset_id
"""

EMBEDDING_EXISTS = f"""
SELECT 1 FROM {catalog.EMBEDDINGS_TABLE}
WHERE asset_id = :asset_id
  AND model_key = :model_key
  AND model_version = :model_version
"""

INSERT_EMBEDDING = f"""
INSERT INTO {catalog.EMBEDDINGS_TABLE} (
    asset_id, model_key, model_version, dimensions, embedding
) VALUES (
    :asset_id, :model_key, :model_version, :dimensions,
    CAST(:embedding AS vector)
)
ON CONFLICT (asset_id, model_key, model_version) DO NOTHING
"""

PUBLISH_ASSET = """
UPDATE assets SET status = :ready
WHERE id = :asset_id AND status = :stored
"""

SUCCEED_JOB = """
UPDATE jobs SET
    state = :succeeded,
    leased_until = NULL,
    claim_token = NULL,
    next_run_at = NULL,
    last_error = NULL,
    updated_at = now()
WHERE id = :job_id
"""

ASSETS_MISSING_EMBEDDING = f"""
SELECT assets.id, assets.status, assets.sha256,
       assets.original_key, assets.thumbnail_key
FROM assets
WHERE assets.status = ANY(:present)
  AND NOT EXISTS (
      SELECT 1 FROM {catalog.EMBEDDINGS_TABLE} AS embedding
      WHERE embedding.asset_id = assets.id
        AND embedding.model_key = :model_key
        AND embedding.model_version = :model_version
  )
ORDER BY assets.ingested_at, assets.id
"""


def asset_params(asset_id: UUID) -> dict:
    return {"asset_id": asset_id}


def identity_params(asset_id: UUID, model_key: str, model_version: str) -> dict:
    return {**asset_params(asset_id), "model_key": model_key, "model_version": model_version}


def embedding_params(
    asset_id: UUID, model_key: str, model_version: str, dimensions: int, vector: list[float]
) -> dict:
    return {
        **identity_params(asset_id, model_key, model_version),
        "dimensions": dimensions,
        "embedding": "[" + ",".join(format(value, ".8g") for value in vector) + "]",
    }


def publish_params(asset_id: UUID) -> dict:
    return {"asset_id": asset_id, "ready": catalog.ASSET_READY, "stored": catalog.ASSET_STORED}


def succeed_params(job_id: UUID) -> dict:
    return {"job_id": job_id, "succeeded": catalog.JOB_SUCCEEDED}


def missing_params(model_key: str, model_version: str) -> dict:
    return {
        "present": [catalog.ASSET_STORED, catalog.ASSET_READY],
        "model_key": model_key,
        "model_version": model_version,
    }
