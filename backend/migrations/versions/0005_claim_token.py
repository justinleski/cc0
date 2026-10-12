"""Fence worker writes with a per-claim token.

Revision ID: 0005_claim_token
Revises: 0004_decision_reason
Create Date: 2026-10-12

Claiming a job was already atomic, but every write afterwards was keyed by job
id alone. A worker whose lease expired mid-job could still overwrite the state
of the worker that reclaimed and finished it. The token issued at claim time is
what a later write proves it still holds, so a stale worker updates zero rows
instead of the wrong ones.

Rows that are leased when this runs belong to no live claim, so they get a
fresh token that no worker holds: their next write fails the fence and the
lease expires normally.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import catalog
from app.db.roles import quote_literal

revision: str = "0005_claim_token"
down_revision: str | None = "0004_decision_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEASED_HAS_TOKEN = "jobs_leased_has_claim_token"


def upgrade() -> None:
    leased = quote_literal(catalog.JOB_LEASED)
    op.add_column(
        "jobs",
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"UPDATE jobs SET claim_token = gen_random_uuid()"
        f" WHERE state = {leased} AND claim_token IS NULL"
    )
    op.create_check_constraint(
        _LEASED_HAS_TOKEN,
        "jobs",
        f"state <> {leased} OR claim_token IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_LEASED_HAS_TOKEN, "jobs", type_="check")
    op.drop_column("jobs", "claim_token")
