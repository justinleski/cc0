"""Record why each rights decision came out the way it did.

Revision ID: 0004_decision_reason
Revises: 0003_catalog
Create Date: 2026-10-12

`raw_statement` holds the provider's own words. This column holds ours: the
stable token the rights gate produced, such as `restrictive_statement` or
`untrusted_public_domain_flag`. Without it an audit has to re-derive the
reasoning from a free-text quote, and the token is the part that stays
comparable as the gate's wording changes.

Existing rows predate the token and get the empty default rather than a guess.
A new column inherits the table grants from 0003, so no grant changes here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_decision_reason"
down_revision: str | None = "0003_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rights_evidence",
        sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("rights_evidence", "decision_reason")
