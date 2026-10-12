"""Grant access to future tables and sequences for both migration creators."""

from alembic import op

from app.db.roles import default_privilege_statements

revision = "0002_defaults"
down_revision = "0001_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in default_privilege_statements("GRANT", "TO"):
        op.execute(statement)


def downgrade() -> None:
    for statement in default_privilege_statements("REVOKE", "FROM"):
        op.execute(statement)
