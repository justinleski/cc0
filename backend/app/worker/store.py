"""Embedding persistence retained by the worker during incremental feature extraction."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, text

from app.db import catalog
from app.worker.jobs import ClaimedJob, hold_claim


@dataclass(frozen=True)
class Asset:
    id: UUID
    status: str
    sha256: str
    original_key: str
    thumbnail_key: str


class CatalogStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_asset_by_id(self, asset_id: UUID) -> Asset | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, status, sha256, original_key, thumbnail_key FROM assets
                    WHERE id = :asset_id
                    """
                ),
                {"asset_id": asset_id},
            ).one_or_none()
        if row is None:
            return None
        return Asset(
            id=row.id,
            status=row.status,
            sha256=row.sha256,
            original_key=row.original_key,
            thumbnail_key=row.thumbnail_key,
        )

    def embedding_exists(self, asset_id: UUID, model_key: str, model_version: str) -> bool:
        with self._engine.begin() as connection:
            found = connection.execute(
                text(
                    f"""
                    SELECT 1 FROM {catalog.EMBEDDINGS_TABLE}
                    WHERE asset_id = :asset_id
                      AND model_key = :model_key
                      AND model_version = :model_version
                    """
                ),
                {
                    "asset_id": asset_id,
                    "model_key": model_key,
                    "model_version": model_version,
                },
            ).scalar_one_or_none()
        return found is not None

    def publish_existing_embedding(self, job: ClaimedJob, asset_id: UUID) -> None:
        """Mark READY when this checkpoint's vector is already stored.

        Does not write a vector. A retried job must not replace the row that
        the first attempt committed.
        """
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            connection.execute(
                text(
                    """
                    UPDATE assets SET status = :ready
                    WHERE id = :asset_id AND status = :stored
                    """
                ),
                {
                    "asset_id": asset_id,
                    "ready": catalog.ASSET_READY,
                    "stored": catalog.ASSET_STORED,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :succeeded,
                        leased_until = NULL,
                        claim_token = NULL,
                        next_run_at = NULL,
                        last_error = NULL,
                        updated_at = now()
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job.id, "succeeded": catalog.JOB_SUCCEEDED},
            )

    def save_embedding(
        self,
        *,
        job: ClaimedJob,
        asset_id: UUID,
        model_key: str,
        model_version: str,
        dimensions: int,
        vector: list[float],
    ) -> None:
        """Insert the vector and publish the asset. The vector row is not updated.

        A second attempt that finds the row already written still moves a
        STORED asset to READY. An asset that is already READY stays READY, so
        a later model version does not unpublish the previous one.
        """
        literal = "[" + ",".join(format(value, ".8g") for value in vector) + "]"
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            connection.execute(
                text(
                    f"""
                    INSERT INTO {catalog.EMBEDDINGS_TABLE} (
                        asset_id, model_key, model_version, dimensions, embedding
                    ) VALUES (
                        :asset_id, :model_key, :model_version, :dimensions,
                        CAST(:embedding AS vector)
                    )
                    ON CONFLICT (asset_id, model_key, model_version) DO NOTHING
                    """
                ),
                {
                    "asset_id": asset_id,
                    "model_key": model_key,
                    "model_version": model_version,
                    "dimensions": dimensions,
                    "embedding": literal,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE assets SET status = :ready
                    WHERE id = :asset_id AND status = :stored
                    """
                ),
                {
                    "asset_id": asset_id,
                    "ready": catalog.ASSET_READY,
                    "stored": catalog.ASSET_STORED,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE jobs SET
                        state = :succeeded,
                        leased_until = NULL,
                        claim_token = NULL,
                        next_run_at = NULL,
                        last_error = NULL,
                        updated_at = now()
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job.id, "succeeded": catalog.JOB_SUCCEEDED},
            )

    def assets_missing_embedding(self, model_key: str, model_version: str) -> list[Asset]:
        """STORED or READY assets with no row for this checkpoint."""
        with self._engine.begin() as connection:
            rows = connection.execute(
                text(
                    f"""
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
                ),
                {
                    "present": [catalog.ASSET_STORED, catalog.ASSET_READY],
                    "model_key": model_key,
                    "model_version": model_version,
                },
            ).all()
        return [
            Asset(
                id=row.id,
                status=row.status,
                sha256=row.sha256,
                original_key=row.original_key,
                thumbnail_key=row.thumbnail_key,
            )
            for row in rows
        ]
