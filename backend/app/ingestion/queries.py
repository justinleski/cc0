"""Pure ingestion SQL construction. All caller-controlled values are bound parameters."""

import json
from typing import Any
from uuid import UUID

from app.db import catalog

_SOURCE_COLUMNS = """
    id, provider_id, external_id, landing_url, original_url, raw_metadata,
    rights_classification, eligibility_status, next_recheck_at,
    (next_recheck_at IS NOT NULL AND next_recheck_at <= now()) AS recheck_due
"""


APPEND_EVIDENCE = """
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


SUPPRESS_ASSET = """
UPDATE assets
   SET status = :suppressed
 WHERE source_record_id = :source_record_id
   AND status <> :suppressed
"""


ENSURE_PROVIDER = """
INSERT INTO providers (name, base_url, policy_url, rate_limit_rps)
VALUES (:name, :base_url, :policy_url, :rate_limit_rps)
ON CONFLICT (name) DO UPDATE
    SET base_url = EXCLUDED.base_url,
        policy_url = EXCLUDED.policy_url,
        rate_limit_rps = EXCLUDED.rate_limit_rps,
        updated_at = now()
RETURNING id
"""


UPSERT_SOURCE_RECORD = f"""
INSERT INTO source_records (provider_id, external_id)
VALUES (:provider_id, :external_id)
ON CONFLICT (provider_id, external_id) DO UPDATE
    SET external_id = EXCLUDED.external_id
RETURNING {_SOURCE_COLUMNS}
"""


GET_SOURCE_RECORD = f"""
SELECT {_SOURCE_COLUMNS} FROM source_records
WHERE provider_id = :provider_id AND external_id = :external_id
"""


UPDATE_RIGHTS = """
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


LATEST_EVIDENCE = """
SELECT rights_type, verification_status, verification_method, checked_at
FROM rights_evidence
WHERE source_record_id = :source_record_id
ORDER BY checked_at DESC, id DESC
LIMIT 1
"""


EVIDENCE_COUNT = """
SELECT count(*) FROM rights_evidence
WHERE source_record_id = :source_record_id
"""


GET_ASSET = """
SELECT id, status, sha256, original_key, thumbnail_key FROM assets
WHERE source_record_id = :source_record_id
"""


UPSERT_CREATOR = """
INSERT INTO creators (provider_id, external_key, name, external_ids)
VALUES (:provider_id, :external_key, :name, CAST(:external_ids AS jsonb))
ON CONFLICT (provider_id, external_key) DO UPDATE
    SET name = EXCLUDED.name,
        external_ids = creators.external_ids || EXCLUDED.external_ids
RETURNING id
"""


INSERT_ASSET = """
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


ENQUEUE_EMBED = """
INSERT INTO jobs (kind, dedupe_key, state, payload, max_attempts)
VALUES (
    :kind, :dedupe_key, :state, CAST(:payload AS jsonb), :max_attempts
)
ON CONFLICT (dedupe_key) DO NOTHING
"""


SUCCEED_INGEST = """
UPDATE jobs SET
    state = :succeeded,
    leased_until = NULL,
    claim_token = NULL,
    next_run_at = NULL,
    last_error = NULL,
    updated_at = now()
WHERE id = :job_id
"""


def rights_update_query(
    source_record_id: UUID,
    landing_url: str | None,
    original_url: str | None,
    raw_metadata: dict[str, Any],
    classification: str,
    eligibility: str,
    recheck_days: int | None,
) -> tuple[str, dict[str, Any]]:
    return UPDATE_RIGHTS, {
        "id": source_record_id,
        "landing_url": landing_url,
        "original_url": original_url,
        "raw_metadata": json.dumps(raw_metadata),
        "classification": classification,
        "eligibility": eligibility,
        "recheck_days": recheck_days,
    }


def embed_job_query(
    asset_id: UUID,
    model_key: str,
    model_version: str,
    original_key: str,
    dedupe: str,
    max_attempts: int,
) -> tuple[str, dict[str, Any]]:
    return ENQUEUE_EMBED, {
        "kind": catalog.JOB_KIND_EMBED,
        "dedupe_key": dedupe,
        "state": catalog.JOB_PENDING,
        "payload": json.dumps(
            {
                "asset_id": str(asset_id),
                "model_key": model_key,
                "model_version": model_version,
                "original_key": original_key,
            }
        ),
        "max_attempts": max_attempts,
    }


def creator_query(
    provider_id: UUID,
    creator_external_key: str,
    creator_name: str | None,
    creator_external_ids: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    return UPSERT_CREATOR, {
        "provider_id": provider_id,
        "external_key": creator_external_key,
        "name": creator_name or creator_external_key,
        "external_ids": json.dumps(creator_external_ids),
    }
