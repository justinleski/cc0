"""Ingestion persistence; atomic catalog and job writes share one transaction."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.db import catalog
from app.ingestion import queries
from app.ingestion.models import Asset, Evidence, SourceRecord
from app.worker.jobs import ClaimedJob, embed_dedupe_key, hold_claim


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
        text(queries.APPEND_EVIDENCE),
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
    if rights_type == catalog.RIGHTS_CC0 and verification_status == catalog.VERIFICATION_VERIFIED:
        return
    connection.execute(
        text(queries.SUPPRESS_ASSET),
        {
            "source_record_id": source_record_id,
            "suppressed": catalog.ASSET_SUPPRESSED,
        },
    )


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


class IngestionRepository:
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
                text(queries.ENSURE_PROVIDER),
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
                text(queries.UPSERT_SOURCE_RECORD),
                {"provider_id": provider_id, "external_id": external_id},
            ).one()
        return _source_record(row)

    def get_source_record(self, provider_id: UUID, external_id: str) -> SourceRecord | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(queries.GET_SOURCE_RECORD),
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
            hold_claim(connection, job)
            statement, params = queries.rights_update_query(
                record.id,
                landing_url,
                original_url,
                raw_metadata,
                classification,
                eligibility,
                recheck_days,
            )
            connection.execute(text(statement), params)
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
                text(queries.LATEST_EVIDENCE),
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
                text(queries.EVIDENCE_COUNT),
                {"source_record_id": source_record_id},
            ).scalar_one()

    def get_asset(self, source_record_id: UUID) -> Asset | None:
        with self._engine.begin() as connection:
            row = connection.execute(
                text(queries.GET_ASSET),
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
            hold_claim(connection, job)
            creator_id = None
            if creator_external_key is not None:
                statement, params = queries.creator_query(
                    provider_id, creator_external_key, creator_name, creator_external_ids
                )
                creator_id = connection.execute(text(statement), params).scalar_one()
            asset_id = connection.execute(
                text(queries.INSERT_ASSET),
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
                statement, params = queries.embed_job_query(
                    asset_id,
                    model_key,
                    model_version,
                    original_key,
                    embed_dedupe_key(model_key, model_version, asset_id),
                    embed_max_attempts,
                )
                connection.execute(text(statement), params)
            connection.execute(
                text(queries.SUCCEED_INGEST),
                {"job_id": job.id, "succeeded": catalog.JOB_SUCCEEDED},
            )
            return asset_id
