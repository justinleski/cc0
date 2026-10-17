"""Private boards and local identity persistence; no worker access.

Revision ID: 0007_boards
Revises: 0006_embeddings
"""

from alembic import op

from app.db import roles
from app.db.roles import quote_identifier

revision = "0007_boards"
down_revision = "0006_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            auth_subject text NOT NULL UNIQUE CHECK (length(auth_subject) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE boards (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id uuid NOT NULL REFERENCES users(id),
            name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
            description text NOT NULL DEFAULT '' CHECK (length(description) <= 5000),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX boards_owner_created_idx ON boards(owner_user_id, created_at DESC, id)"
    )
    op.execute("""
        CREATE TABLE board_items (
            board_id uuid NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
            asset_id uuid NOT NULL REFERENCES assets(id),
            position integer NOT NULL CHECK (position >= 0),
            notes text NOT NULL DEFAULT '' CHECK (length(notes) <= 5000),
            PRIMARY KEY (board_id, asset_id),
            CONSTRAINT board_items_position_unique UNIQUE (board_id, position)
                DEFERRABLE INITIALLY DEFERRED
        )
    """)
    op.execute("CREATE INDEX board_items_asset_idx ON board_items(asset_id)")
    api = quote_identifier(roles.ROLE_API)
    op.execute(f"GRANT SELECT, INSERT ON users TO {api}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON boards, board_items TO {api}")


def downgrade() -> None:
    op.execute("DROP TABLE board_items")
    op.execute("DROP TABLE boards")
    op.execute("DROP TABLE users")
