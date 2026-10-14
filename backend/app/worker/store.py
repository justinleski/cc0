"""Catalog reads and writes for the ingestion worker.

Three rules shape this module. Rights evidence is append-only: a recheck inserts
a row, it never edits the previous answer, which is what makes the history
auditable. The asset row is inserted only in the transaction that marks it
STORED, after both objects are written, so a failed job can never leave a
half-published asset behind. And every write a worker makes happens under its
claim: the job row is locked and its token checked first, so a worker whose
lease expired cannot record a decision the job's current owner would contradict.

Committing the asset and the job's success together is what keeps a crash on
the last attempt from turning finished work into a dead letter.

Timestamps come from `now()` in the database, not from the worker process, so a
recheck window does not depend on host clock skew.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.db import catalog
from app.worker.jobs import CLAIM_HELD, ClaimedJob, ClaimLost, embed_dedupe_key


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


def _append_evidence(
    connection: Connection,
    *,
    source_record_id: UUID,
    rights_type: str,
    verification_status: str,
    verification_method: str,
    evidence_url: str | None,
    raw_statement: str,
    decision_reason: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO rights_evidence (
                source_record_id, rights_type, verification_status,
                evidence_url, raw_statement, verification_method,
                decision_reason
            ) VALUES (
                :source_record_id, :rights_type, :verification_status,
                :evidence_url, :raw_statement, :verification_method,
                :decision_reason
            )
            """
        ),
        {
            "source_record_id": source_record_id,
            "rights_type": rights_type,
            "verification_status": verification_status,
            "evidence_url": evidence_url,
            "raw_statement": raw_statement,
            "verification_method": verification_method,
            "decision_reason": decision_reason,
        },
    )


def _suppress_unless_verified_cc0(
    connection: Connection,
    source_record_id: UUID,
    rights_type: str,
    verification_status: str,
) -> None:
    """Unpublish when this decision is anything other than verified CC0.

    Runs in the same transaction as the evidence insert. A verified CC0 row
    leaves status unchanged.
    """
    if (
        rights_type == catalog.RIGHTS_CC0
        and verification_status == catalog.VERIFICATION_VERIFIED
    ):
        return
    connection.execute(
        text(
            """
            UPDATE assets
               SET status = :suppressed
             WHERE source_record_id = :source_record_id
               AND status <> :suppressed
            """
        ),
        {
            "source_record_id": source_record_id,
            "suppressed": catalog.ASSET_SUPPRESSED,
        },
    )


def _hold_claim(connection: Connection, job: ClaimedJob) -> None:
    """Lock the job row for this transaction, or refuse to write at all.

    `FOR UPDATE` keeps the row from being reclaimed between this check and the
    commit, so the catalog write and the job state it belongs to settle on the
    same answer about who owns the work.
    """
    held = connection.execute(
        text(f"SELECT id FROM jobs WHERE {CLAIM_HELD} FOR UPDATE"),
        {
            "job_id": job.id,
            "claim_token": job.claim_token,
            "leased": catalog.JOB_LEASED,
        },
    ).scalar_one_or_none()
    if held is None:
        raise ClaimLost(job.id)


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
        job: ClaimedJob,
        classification: str,
        eligibility: str,
        verification_status: str,
        verification_method: str,
        evidence_url: str | None,
        raw_statement: str,
        decision_reason: str,
        landing_url: str | None,
        original_url: str | None,
        raw_metadata: dict[str, Any],
        recheck_days: int | None,
    ) -> None:
        """Append evidence and update the record in one transaction.

        `recheck_days` is None for an eligible record: M1 does not revisit a
        verified CC0 decision, so it gets no recheck deadline and a repeat run
        cannot be mistaken for a due recheck.

        Raises ClaimLost when this worker no longer holds the job, so an
        expired attempt cannot overwrite a rights answer someone else recorded.
        """
        with self._engine.begin() as connection:
            _hold_claim(connection, job)
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
            _append_evidence(
                connection,
                source_record_id=record.id,
                rights_type=classification,
                verification_status=verification_status,
                verification_method=verification_method,
                evidence_url=evidence_url,
                raw_statement=raw_statement,
                decision_reason=decision_reason,
            )
            _suppress_unless_verified_cc0(
                connection, record.id, classification, verification_status
            )

    def record_superseding_rights(
        self,
        source_record_id: UUID,
        *,
        rights_type: str,
        verification_status: str,
        verification_method: str,
        evidence_url: str | None,
        raw_statement: str,
        decision_reason: str,
    ) -> None:
        """Append a rights decision and unpublish when it is not verified CC0.

        Ingest treats an asset that already has bytes as finished
        (`ASSET_HAS_BYTES` includes STORED, READY, and SUPPRESSED) and does not
        re-run the rights gate. A withdrawal after publish goes through this
        function. The evidence insert and the status change commit together.
        A verified CC0 row is appended and leaves status unchanged.
        """
        with self._engine.begin() as connection:
            _append_evidence(
                connection,
                source_record_id=source_record_id,
                rights_type=rights_type,
                verification_status=verification_status,
                verification_method=verification_method,
                evidence_url=evidence_url,
                raw_statement=raw_statement,
                decision_reason=decision_reason,
            )
            _suppress_unless_verified_cc0(
                connection, source_record_id, rights_type, verification_status
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
        job: ClaimedJob,
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
        model_key: str,
        model_version: str,
        embed_max_attempts: int,
    ) -> UUID | None:
        """Insert the asset as STORED and finish the job. None when it existed.

        The unique constraint on source_record_id is what makes a repeated
        ingestion idempotent, so the conflict is expected rather than an error.

        The job reaches `succeeded` in this same transaction. Splitting the two
        left a window where a crash after the asset committed dead-lettered
        work that was already done, and a later attempt had to rediscover it.
        """
        with self._engine.begin() as connection:
            _hold_claim(connection, job)
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
            asset_id = connection.execute(
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
            if asset_id is not None:
                # Same transaction as the asset. A crash here does not leave a
                # stored image with no embed job, and the embed job does not
                # re-download if encoding fails.
                connection.execute(
                    text(
                        """
                        INSERT INTO jobs (kind, dedupe_key, state, payload, max_attempts)
                        VALUES (
                            :kind, :dedupe_key, :state, CAST(:payload AS jsonb), :max_attempts
                        )
                        ON CONFLICT (dedupe_key) DO NOTHING
                        """
                    ),
                    {
                        "kind": catalog.JOB_KIND_EMBED,
                        "dedupe_key": embed_dedupe_key(model_key, model_version, asset_id),
                        "state": catalog.JOB_PENDING,
                        "payload": json.dumps(
                            {
                                "asset_id": str(asset_id),
                                "model_key": model_key,
                                "model_version": model_version,
                                "original_key": original_key,
                            }
                        ),
                        "max_attempts": embed_max_attempts,
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
            return asset_id

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
            _hold_claim(connection, job)
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
            _hold_claim(connection, job)
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
