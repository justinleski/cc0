"""Parameterized SQL for the export API. Every lookup is scoped to the owner."""

LOCK_BOARD = """
SELECT id, name FROM boards WHERE id = :board_id AND owner_user_id = :owner_id FOR UPDATE
"""
BOARD_OWNED = "SELECT id FROM boards WHERE id = :board_id AND owner_user_id = :owner_id"
ELIGIBLE_COUNT = """
SELECT count(*) FROM board_items item
JOIN ready_assets asset ON asset.id = item.asset_id
WHERE item.board_id = :board_id
"""
ACTIVE_COUNT = """
SELECT count(*) FROM board_exports WHERE owner_user_id = :owner_id AND status = 'QUEUED'
"""
INSERT_EXPORT = """
INSERT INTO board_exports (owner_user_id, board_id, board_name)
VALUES (:owner_id, :board_id, :board_name)
RETURNING id, board_id, status, failure_reason, included_count, excluded_count,
          created_at, completed_at, expires_at, false AS expired
"""
# Position is renumbered 0..n-1 over the pins that are eligible right now, so the
# snapshot's filename order is stable and gap-free whatever the board looks like.
SNAPSHOT_ITEMS = """
INSERT INTO board_export_items (export_id, asset_id, position)
SELECT :export_id, item.asset_id,
       (row_number() OVER (ORDER BY item.position) - 1)::integer
FROM board_items item
JOIN ready_assets asset ON asset.id = item.asset_id
WHERE item.board_id = :board_id
"""
# The API role has no table access to jobs; this function inserts the one job
# a QUEUED export needs (migration 0010).
INSERT_JOB = "SELECT cc0_enqueue_export_job(:export_id, :max_attempts)"
_COLUMNS = """
id, board_id, status, failure_reason, included_count, excluded_count,
created_at, completed_at, expires_at, (expires_at IS NOT NULL AND expires_at <= now()) AS expired
"""
GET_OWNED = f"""
SELECT {_COLUMNS}, artifact_key, owner_user_id FROM board_exports
WHERE id = :export_id AND owner_user_id = :owner_id
"""
LIST_FOR_BOARD = f"""
SELECT {_COLUMNS} FROM board_exports
WHERE board_id = :board_id AND owner_user_id = :owner_id
ORDER BY created_at DESC, id LIMIT :limit
"""
# Packaged assets that are no longer READY with latest verified CC0 evidence.
SUPPRESSED_SINCE = """
SELECT count(*) FROM board_export_items item
LEFT JOIN ready_assets asset ON asset.id = item.asset_id
WHERE item.export_id = :export_id AND item.packaged AND asset.id IS NULL
"""
