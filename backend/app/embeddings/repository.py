"""Embedding persistence with claim-fenced publication and job settlement."""

from uuid import UUID

from sqlalchemy import Engine, text

from app.embeddings import queries
from app.embeddings.models import Asset
from app.worker.jobs import ClaimedJob, hold_claim


def _asset(row) -> Asset:
    return Asset(
        id=row.id,
        status=row.status,
        sha256=row.sha256,
        original_key=row.original_key,
        thumbnail_key=row.thumbnail_key,
    )


class EmbeddingRepository:
    def __init__(self, engine: Engine) -> None:
        # Inference and object reads must complete before a write connection is checked out.
        self._engine = engine

    def get_asset_by_id(self, asset_id: UUID) -> Asset | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(queries.ASSET_BY_ID), queries.asset_params(asset_id)
            ).one_or_none()
        return _asset(row) if row is not None else None

    def embedding_exists(self, asset_id: UUID, model_key: str, model_version: str) -> bool:
        with self._engine.begin() as connection:
            found = connection.execute(
                text(queries.EMBEDDING_EXISTS),
                queries.identity_params(asset_id, model_key, model_version),
            ).scalar_one_or_none()
        return found is not None

    def publish_existing_embedding(self, job: ClaimedJob, asset_id: UUID) -> None:
        """Set READY and settle the job without replacing the existing vector."""
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            connection.execute(text(queries.PUBLISH_ASSET), queries.publish_params(asset_id))
            connection.execute(text(queries.SUCCEED_JOB), queries.succeed_params(job.id))

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
        """Insert the vector, set READY, and settle the job in one fenced transaction."""
        params = queries.embedding_params(asset_id, model_key, model_version, dimensions, vector)
        with self._engine.begin() as connection:
            hold_claim(connection, job)
            connection.execute(text(queries.INSERT_EMBEDDING), params)
            connection.execute(text(queries.PUBLISH_ASSET), queries.publish_params(asset_id))
            connection.execute(text(queries.SUCCEED_JOB), queries.succeed_params(job.id))

    def assets_missing_embedding(self, model_key: str, model_version: str) -> list[Asset]:
        """Find STORED or READY assets with no row for this checkpoint."""
        with self._engine.begin() as connection:
            rows = connection.execute(
                text(queries.ASSETS_MISSING_EMBEDDING),
                queries.missing_params(model_key, model_version),
            ).all()
        return [_asset(row) for row in rows]
