"""SQL for the export worker. It reads the catalog directly and never touches boards."""

EXPORT = """
SELECT id, owner_user_id, board_id, board_name, status, created_at
FROM board_exports WHERE id = :export_id
"""
LOCK_EXPORT = EXPORT + " FOR UPDATE"

# Eligibility is the same rule as the ready_assets view, evaluated against the
# latest evidence row: READY, and the newest decision is verified CC0.
ITEMS = """
SELECT item.asset_id, item.position,
       asset.title, asset.sha256, asset.mime_type, asset.width, asset.height,
       asset.original_key, creator.name AS creator_name,
       provider.name AS provider_name, source.external_id, source.landing_url,
       source.original_url, source.last_fetched_at,
       evidence.rights_type, evidence.verification_status, evidence.verification_method,
       evidence.evidence_url, evidence.raw_statement, evidence.checked_at AS verified_at,
       (asset.status = 'READY' AND evidence.rights_type = 'CC0'
        AND evidence.verification_status = 'VERIFIED') AS eligible
FROM board_export_items item
JOIN assets asset ON asset.id = item.asset_id
JOIN source_records source ON source.id = asset.source_record_id
JOIN providers provider ON provider.id = source.provider_id
LEFT JOIN creators creator ON creator.id = asset.creator_id
LEFT JOIN LATERAL (
    SELECT rights_type, verification_status, verification_method, evidence_url,
           raw_statement, checked_at
    FROM rights_evidence
    WHERE rights_evidence.source_record_id = asset.source_record_id
    ORDER BY checked_at DESC, id DESC LIMIT 1
) evidence ON true
WHERE item.export_id = :export_id
ORDER BY item.position
"""
# Taken before the final eligibility read. A rights decision rewrites the asset
# row, so it either commits before this lock or waits until the export is
# published, when the API's download-time recheck takes over.
LOCK_ASSETS = "SELECT id FROM assets WHERE id = ANY(:asset_ids) ORDER BY id FOR SHARE"
STILL_ELIGIBLE = """
SELECT asset.id
FROM assets asset
JOIN LATERAL (
    SELECT rights_type, verification_status FROM rights_evidence
    WHERE rights_evidence.source_record_id = asset.source_record_id
    ORDER BY checked_at DESC, id DESC LIMIT 1
) evidence ON true
WHERE asset.id = ANY(:asset_ids) AND asset.status = 'READY'
  AND evidence.rights_type = 'CC0' AND evidence.verification_status = 'VERIFIED'
"""
MARK_PACKAGED = """
UPDATE board_export_items SET packaged = true
WHERE export_id = :export_id AND asset_id = ANY(:asset_ids)
"""
MARK_EXCLUDED = """
UPDATE board_export_items SET excluded_reason = :reason
WHERE export_id = :export_id AND asset_id = :asset_id
"""
PUBLISH = """
UPDATE board_exports SET
    status = 'READY', failure_reason = NULL, artifact_key = :artifact_key,
    artifact_sha256 = :sha256, artifact_bytes = :size, included_count = :included,
    excluded_count = :excluded, completed_at = now(),
    expires_at = now() + make_interval(hours => :ttl_hours)
WHERE id = :export_id AND status = 'QUEUED'
"""
FAIL = """
UPDATE board_exports SET status = 'FAILED', failure_reason = :reason, completed_at = now()
WHERE id = :export_id AND status = 'QUEUED'
"""
SUCCEED_JOB = """
UPDATE jobs SET state = 'succeeded', leased_until = NULL, claim_token = NULL,
                next_run_at = NULL, last_error = NULL, updated_at = now()
WHERE id = :job_id
"""
DEAD_JOB = """
UPDATE jobs SET state = 'dead', dead_at = now(), leased_until = NULL, claim_token = NULL,
                next_run_at = NULL, last_error = :reason, updated_at = now()
WHERE id = :job_id
"""
# A job that was dead-lettered by the reaper never ran a settlement transaction.
FAIL_DEAD_EXPORTS = """
UPDATE board_exports SET status = 'FAILED', completed_at = now(),
    failure_reason = COALESCE(
        (SELECT last_error FROM jobs WHERE dedupe_key = 'export:' || board_exports.id::text),
        'attempts_exhausted')
WHERE status = 'QUEUED'
  AND EXISTS (SELECT 1 FROM jobs
              WHERE dedupe_key = 'export:' || board_exports.id::text AND state = 'dead')
"""
# Redriving a dead export job returns the export to the queue.
REQUEUE_REDRIVEN = """
UPDATE board_exports SET status = 'QUEUED', failure_reason = NULL, completed_at = NULL
WHERE status = 'FAILED' AND artifact_key IS NULL
  AND EXISTS (SELECT 1 FROM jobs
              WHERE dedupe_key = 'export:' || board_exports.id::text AND state = 'pending')
"""
DUE_FOR_RETIREMENT = """
SELECT export.id, export.artifact_key,
       (export.expires_at <= now()) AS expired
FROM board_exports export
WHERE export.status = 'READY'
  AND (export.expires_at <= now() OR EXISTS (
        SELECT 1 FROM board_export_items item
        JOIN assets asset ON asset.id = item.asset_id
        LEFT JOIN LATERAL (
            SELECT rights_type, verification_status FROM rights_evidence
            WHERE rights_evidence.source_record_id = asset.source_record_id
            ORDER BY checked_at DESC, id DESC LIMIT 1
        ) evidence ON true
        WHERE item.export_id = export.id AND item.packaged
          AND NOT (asset.status = 'READY' AND evidence.rights_type = 'CC0'
                   AND evidence.verification_status = 'VERIFIED')))
ORDER BY export.expires_at LIMIT :limit
"""
RETIRE = """
UPDATE board_exports SET status = :status, failure_reason = :reason
WHERE id = :export_id AND status = 'READY'
"""
REFERENCED_KEYS = "SELECT artifact_key FROM board_exports WHERE artifact_key IS NOT NULL"
