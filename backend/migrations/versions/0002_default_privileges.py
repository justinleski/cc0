"""Grant access to future tables and sequences for both migration creators."""

from alembic import op

from app.db import roles
from app.db.roles import quote_identifier

revision = "0002_defaults"
down_revision = "0001_roles"
branch_labels = None
depends_on = None


def _change_defaults(action: str, direction: str) -> None:
    # Role names are read at call time so a test suffix applied after import
    # is the name that actually runs. See 0001.
    recipients = f"{quote_identifier(roles.ROLE_API)}, {quote_identifier(roles.ROLE_WORKER)}"
    # Unqualified defaults apply to the current Alembic superuser. Defaults are
    # creator-specific, so also cover tables created by the restricted migrator.
    for creator in ("", f"FOR ROLE {quote_identifier(roles.ROLE_MIGRATOR)} "):
        for kind, privileges in (
            ("TABLES", "SELECT, INSERT, UPDATE, DELETE"),
            ("SEQUENCES", "USAGE, SELECT"),
        ):
            op.execute(
                f"ALTER DEFAULT PRIVILEGES {creator}IN SCHEMA public "
                f"{action} {privileges} ON {kind} {direction} {recipients}"
            )


def upgrade() -> None:
    _change_defaults("GRANT", "TO")


def downgrade() -> None:
    _change_defaults("REVOKE", "FROM")
