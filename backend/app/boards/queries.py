"""Parameterized SQL; every board lookup is scoped to the authenticated owner."""

OWNED = "SELECT * FROM boards WHERE id = :board_id AND owner_user_id = :owner_id"
LOCK_OWNED = OWNED + " FOR UPDATE"
READ_OWNED = OWNED + " FOR SHARE"
LIST = """
SELECT * FROM boards WHERE owner_user_id = :owner_id
ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset
"""
CREATE = """
INSERT INTO boards (owner_user_id, name, description)
VALUES (:owner_id, :name, :description) RETURNING *
"""
EDIT = """
UPDATE boards SET name = COALESCE(:name, name), description = COALESCE(:description, description),
                  updated_at = clock_timestamp()
WHERE id = :board_id AND owner_user_id = :owner_id
"""
DELETE = "DELETE FROM boards WHERE id = :board_id AND owner_user_id = :owner_id"
TOUCH = """
UPDATE boards SET updated_at = clock_timestamp()
WHERE id = :board_id AND owner_user_id = :owner_id
"""
# These item queries execute only after the owner-scoped parent row is locked.
# Include ownership in item SQL as well so it cannot become an unscoped primitive.
ITEMS = """
SELECT item.asset_id, item.notes,
       (row_number() OVER (ORDER BY item.position) - 1)::integer AS position
FROM board_items item
JOIN boards board ON board.id = item.board_id
JOIN ready_assets asset ON asset.id = item.asset_id
WHERE board.id = :board_id AND board.owner_user_id = :owner_id
ORDER BY item.position
"""
ALL_ITEMS = """
SELECT item.asset_id, item.position, (asset.id IS NOT NULL) AS available
FROM board_items item
JOIN boards board ON board.id = item.board_id
LEFT JOIN ready_assets asset ON asset.id = item.asset_id
WHERE board.id = :board_id AND board.owner_user_id = :owner_id
ORDER BY item.position
"""
ADD = """
INSERT INTO board_items (board_id, asset_id, position, notes)
SELECT board.id, asset.id,
       (SELECT COALESCE(max(position) + 1, 0) FROM board_items WHERE board_id = board.id), :notes
FROM boards board CROSS JOIN ready_assets asset
WHERE board.id = :board_id AND board.owner_user_id = :owner_id AND asset.id = :asset_id
ON CONFLICT (board_id, asset_id) DO NOTHING
RETURNING asset_id
"""
REMOVE = """
DELETE FROM board_items item USING boards board
WHERE item.board_id = board.id AND board.id = :board_id
  AND board.owner_user_id = :owner_id AND item.asset_id = :asset_id
RETURNING item.asset_id
"""
NOTES = """
UPDATE board_items item SET notes = :notes FROM boards board
WHERE item.board_id = board.id AND board.id = :board_id
  AND board.owner_user_id = :owner_id AND item.asset_id = :asset_id
RETURNING item.asset_id
"""
POSITION = """
UPDATE board_items item SET position = :position FROM boards board
WHERE item.board_id = board.id AND board.id = :board_id
  AND board.owner_user_id = :owner_id AND item.asset_id = :asset_id
"""
