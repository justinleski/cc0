"""Enable vector and create local database roles.

Revision ID: 0001_roles
Revises:
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import Settings
from app.db.roles import (
    ROLE_API,
    ROLE_MIGRATOR,
    ROLE_NAMES,
    ROLE_WORKER,
    ensure_login_sql,
    grant_connect_sql,
    quote_identifier,
    revoke_connect_sql,
)

revision: str = "0001_roles"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _passwords() -> dict[str, str]:
    settings = Settings()
    return {
        ROLE_MIGRATOR: settings.cc0_migrator_password,
        ROLE_API: settings.cc0_api_password,
        ROLE_WORKER: settings.cc0_worker_password,
    }


def _database_name() -> str:
    name = op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
    return quote_identifier(name)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    database = _database_name()
    for role, password in _passwords().items():
        # Plain strings: a password can contain ':' which SQLAlchemy text() treats as a bind.
        op.execute(ensure_login_sql(role, password))
        op.execute(grant_connect_sql(database, role))
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {ROLE_MIGRATOR}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE_API}, {ROLE_WORKER}")


def downgrade() -> None:
    database = _database_name()
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE_MIGRATOR}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE_API}, {ROLE_WORKER}")
    for role in ROLE_NAMES:
        op.execute(revoke_connect_sql(database, role))
        op.execute(f"DROP ROLE IF EXISTS {role}")
    op.execute("DROP EXTENSION IF EXISTS vector")
