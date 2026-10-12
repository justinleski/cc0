"""Catalog reads and writes for the ingestion worker.

Two rules shape this module. Rights evidence is append-only: a recheck inserts
a row, it never edits the previous answer, which is what makes the history
auditable. And the asset row is inserted only in the transaction that marks it
STORED, after both objects are written, so a failed job can never leave a
half-published asset behind.

Timestamps come from `now()` in the database, not from the worker process, so a
recheck window does not depend on host clock skew.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.db import catalog


@dataclass(frozen=True)
class SourceRecord:
    id: UUID
    provider_id: UUID
    external_id: str
    landing_url: str | None
    original_url: str | None
    # The allowlisted provider fields, kept so a retry can rebuild the asset
    # columns without asking the provider for metadata it already validated.
    raw_metadata: dict[str, Any]
    rights_classification: str
    eligibility_status: str
    next_recheck_at: datetime | None
    # Evaluated in SQL against now() so the decision uses the database clock.
    recheck_due: bool

    @property
    def is_negative_cached(self) -> bool:
        return self.eligibility_status in catalog.CACHED_STATUSES

    @property
    def is_fresh_negative_cache(self) -> bool:
        """True when a repeat discovery must stop without any request."""
        return self.is_negative_cached and not self.recheck_due


@dataclass(frozen=True)
class Asset:
    id: UUID
    status: str
    sha256: str
    original_key: str
    thumbnail_key: str


@dataclass(frozen=True)
class Evidence:
    rights_type: str
    verification_status: str
    verification_method: str
    checked_at: datetime


_SOURCE_COLUMNS = """
    id, provider_id, external_id, landing_url, original_url, raw_metadata,
    rights_classification, eligibility_status, next_recheck_at,
    (next_recheck_at IS NOT NULL AND next_recheck_at <= now()) AS recheck_due
"""


def _source_record(row: Any) -> SourceRecord:
    return SourceRecord(
        id=row.id,
        provider_id=row.provider_id,
        external_id=row.external_id,
        landing_url=row.landing_url,
        original_url=row.original_url,
        raw_metadata=row.raw_metadata or {},
        rights_classification=row.rights_classification,
        eligibility_status=row.eligibility_status,
        next_recheck_at=row.next_recheck_at,
        recheck_due=row.recheck_due,
    )


class CatalogStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_provider(
        self,
        name: str,
        base_url: str,
        policy_url: str,
        rate_limit_rps: float,
    ) -> UUID:
        with self._engine.begin() as connection:
            return connection.execute(
                text(
                    """
                    INSERT INTO providers (name, base_url, policy_url, rate_limit_rps)
                    VALUES (:name, :base_url, :policy_url, :rate_limit_rps)
                    ON CONFLICT (name) DO UPDATE
                        SET base_url = EXCLUDED.base_url,
                            policy_url = EXCLUDED.policy_url,
                            rate_limit_rps = EXCLUDED.rate_limit_rps,
                            updated_at = now()
                    RETURNING id
                    """
                ),
                {
                    "name": name,
                    "base_url": base_url,
                    "policy_url": policy_url,
                    "rate_limit_rps": rate_limit_rps,
                },
            ).scalar_one()

    def upsert_source_record(self, provider_id: UUID, external_id: str) -> SourceRecord:
        """Create or return the row for (provider, external id).

        The conflict branch re-assigns external_id to its own value: ON CONFLICT
        DO NOTHING would return no row, and this upsert is also the read that
        decides whether the negative cache is still fresh.
        """
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    f"""
                    INSERT INTO source_records (provider_id, external_id)
                    VALUES (:provider_id, :external_id)
                    ON CONFLICT (provider_id, external_id) DO UPDATE
                        SET external_id = EXCLUDED.external_id
                    RETURNING {_SOURCE_COLUMNS}
                    """
                ),
                {"provider_id": provider_id, "external_id": external_id},
            ).one()
        return _source_record(row)

    def get_source_record(self, provider_id: UUID, external_id: str) -> SourceRecord | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT {_SOURCE_COLUMNS} FROM source_records
                    WHERE provider_id = :provider_id AND external_id = :external_id
                    """
                ),
                {"provider_id": provider_id, "external_id": external_id},
            ).one_or_none()
        return None if row is None else _source_record(row)

    def record_rights_decision(
        self,
        record: SourceRecord,
        *,
        classification: str,
        eligibility: str,
        verification_status: str,
        verification_method: str,
        evidence_url: str | None,
        raw_statement: str,
        landing_url: str | None,
        original_url: str | None,
        raw_metadata: dict[str, Any],
        recheck_days: int | None,
    ) -> None:
        """Append evidence and update the record in one transaction.

        `recheck_days` is None for an eligible record: M1 does not revisit a
        verified CC0 decision, so it gets no recheck deadline and a repeat run
        cannot be mistaken for a due recheck.
        """
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE source_records SET
                        landing_url = :landing_url,
                        original_url = :original_url,
                        raw_metadata = CAST(:raw_metadata AS jsonb),
                        rights_classification = :classification,
                        eligibility_status = :eligibility,
                        last_rights_checked_at = now(),
                        last_fetched_at = now(),
                        next_recheck_at = CASE
                            WHEN CAST(:recheck_days AS int) IS NULL THEN NULL
                            ELSE now() + make_interval(days => CAST(:recheck_days AS int))
                        END
                    WHERE id = :id
                    """
                ),
                {
                    "id": record.id,
                    "landing_url": landing_url,
                    "original_url": original_url,
                    "raw_metadata": json.dumps(raw_metadata),
                    "classification": classification,
                    "eligibility": eligibility,
                    "recheck_days": recheck_days,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO rights_evidence (
                        source_record_id, rights_type, verification_status,
                        evidence_url, raw_statement, verification_method
                    ) VALUES (
                        :source_record_id, :rights_type, :verification_status,
                        :evidence_url, :raw_statement, :verification_method
                    )
                    """
                ),
                {
                    "source_record_id": record.id,
                    "rights_type": classification,
                    "verification_status": verification_status,
                    "evidence_url": evidence_url,
                    "raw_statement": raw_statement,
                    "verification_method": verification_method,
                },
            )

    def latest_evidence(self, source_record_id: UUID) -> Evidence | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT rights_type, verification_status, verification_method, checked_at
                    FROM rights_evidence
                    WHERE source_record_id = :source_record_id
                    ORDER BY checked_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"source_record_id": source_record_id},
            ).one_or_none()
        if row is None:
            return None
        return Evidence(
            rights_type=row.rights_type,
            verification_status=row.verification_status,
            verification_method=row.verification_method,
            checked_at=row.checked_at,
        )

    def evidence_count(self, source_record_id: UUID) -> int:
        with self._engine.begin() as connection:
            return connection.execute(
                text(
                    """
                    SELECT count(*) FROM rights_evidence
                    WHERE source_record_id = :source_record_id
                    """
                ),
                {"source_record_id": source_record_id},
            ).scalar_one()

    def get_asset(self, source_record_id: UUID) -> Asset | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, status, sha256, original_key, thumbnail_key FROM assets
                    WHERE source_record_id = :source_record_id
                    """
                ),
                {"source_record_id": source_record_id},
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

    def store_asset(
        self,
        *,
        source_record_id: UUID,
        provider_id: UUID,
        creator_name: str | None,
        creator_external_key: str | None,
        creator_external_ids: dict[str, str],
        title: str | None,
        description: str | None,
        location_label: str | None,
        created_year_start: int | None,
        created_year_end: int | None,
        original_key: str,
        thumbnail_key: str,
        sha256: str,
        mime_type: str,
        width: int,
        height: int,
    ) -> UUID | None:
        """Insert the asset as STORED. Returns None when a row already exists.

        The unique constraint on source_record_id is what makes a repeated
        ingestion idempotent, so the conflict is expected rather than an error.
        """
        with self._engine.begin() as connection:
            creator_id = None
            if creator_external_key is not None:
                creator_id = connection.execute(
                    text(
                        """
                        INSERT INTO creators (provider_id, external_key, name, external_ids)
                        VALUES (:provider_id, :external_key, :name, CAST(:external_ids AS jsonb))
                        ON CONFLICT (provider_id, external_key) DO UPDATE
                            SET name = EXCLUDED.name,
                                external_ids = creators.external_ids || EXCLUDED.external_ids
                        RETURNING id
                        """
                    ),
                    {
                        "provider_id": provider_id,
                        "external_key": creator_external_key,
                        "name": creator_name or creator_external_key,
                        "external_ids": json.dumps(creator_external_ids),
                    },
                ).scalar_one()
            return connection.execute(
                text(
                    """
                    INSERT INTO assets (
                        source_record_id, creator_id, title, description, location_label,
                        created_year_start, created_year_end, original_key, thumbnail_key,
                        sha256, mime_type, width, height, status
                    ) VALUES (
                        :source_record_id, :creator_id, :title, :description, :location_label,
                        :created_year_start, :created_year_end, :original_key, :thumbnail_key,
                        :sha256, :mime_type, :width, :height, :status
                    )
                    ON CONFLICT (source_record_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "source_record_id": source_record_id,
                    "creator_id": creator_id,
                    "title": title,
                    "description": description,
                    "location_label": location_label,
                    "created_year_start": created_year_start,
                    "created_year_end": created_year_end,
                    "original_key": original_key,
                    "thumbnail_key": thumbnail_key,
                    "sha256": sha256,
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                    "status": catalog.ASSET_STORED,
                },
            ).scalar_one_or_none()
