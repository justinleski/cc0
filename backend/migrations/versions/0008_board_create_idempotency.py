"""Owner-scoped, durable identities for board creation requests.

Revision ID: 0008_board_create_idempotency
Revises: 0007_boards
"""

from alembic import op

from app.db import roles
from app.db.roles import quote_identifier

revision = "0008_board_create_idempotency"
down_revision = "0007_boards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE board_create_operations (
            owner_user_id uuid NOT NULL REFERENCES users(id),
            idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
            request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
            board_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_user_id, idempotency_key)
        )
    """)
    api = quote_identifier(roles.ROLE_API)
    op.execute(f"GRANT SELECT, INSERT ON board_create_operations TO {api}")


def downgrade() -> None:
    op.execute("DROP TABLE board_create_operations")
