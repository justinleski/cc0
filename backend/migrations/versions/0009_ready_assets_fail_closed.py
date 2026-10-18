"""Fail closed when rights evidence rows tie on checked_at.

Revision ID: 0009_ready_assets_fail_closed
Revises: 0008_board_create_idempotency
Create Date: 2026-10-18

`rights_evidence.id` is a random UUID, so the `id DESC` tie-break in 0006 picks
an arbitrary row when two decisions share a `checked_at`. A rejection could lose
to a verification. The view now also requires that no row at the latest
`checked_at` is anything other than verified CC0. The columns are unchanged, so
`CREATE OR REPLACE` keeps the `cc0_api` grant.
"""

from collections.abc import Sequence

from alembic import op

from app.db import catalog
from app.db.roles import quote_literal

revision: str = "0009_ready_assets_fail_closed"
down_revision: str | None = "0008_board_create_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _view(tie_guard: str) -> str:
    ready = quote_literal(catalog.ASSET_READY)
    cc0 = quote_literal(catalog.RIGHTS_CC0)
    verified = quote_literal(catalog.VERIFICATION_VERIFIED)
    return f"""
        CREATE OR REPLACE VIEW {catalog.READY_ASSETS_VIEW}
        WITH (security_invoker = false) AS
        SELECT
            assets.id,
            assets.id AS asset_id,
            assets.title,
            assets.description,
            assets.location_label,
            assets.created_year_start,
            assets.created_year_end,
            assets.style_tags,
            assets.original_key,
            assets.thumbnail_key,
            assets.width,
            assets.height,
            assets.ingested_at,
            assets.creator_id,
            creators.name AS creator_name,
            assets.lexeme_simple,
            assets.lexeme_english,
            source_records.landing_url,
            evidence.rights_type,
            evidence.evidence_url,
            evidence.checked_at AS verified_at
        FROM assets
        JOIN source_records ON source_records.id = assets.source_record_id
        JOIN LATERAL (
            SELECT rights_type, verification_status, evidence_url, checked_at
            FROM rights_evidence
            WHERE rights_evidence.source_record_id = assets.source_record_id
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
        ) AS evidence ON true
        LEFT JOIN creators ON creators.id = assets.creator_id
        WHERE assets.status = {ready}
          AND evidence.rights_type = {cc0}
          AND evidence.verification_status = {verified}
          {tie_guard.format(cc0=cc0, verified=verified)}
        """


# Any other decision recorded at the same instant as the chosen row hides the
# asset, whichever UUID sorts first.
_TIE_GUARD = """
          AND NOT EXISTS (
              SELECT 1
              FROM rights_evidence AS tied
              WHERE tied.source_record_id = assets.source_record_id
                AND tied.checked_at = evidence.checked_at
                AND (tied.rights_type <> {cc0} OR tied.verification_status <> {verified})
          )
"""


def upgrade() -> None:
    op.execute(_view(_TIE_GUARD))


def downgrade() -> None:
    op.execute(_view(""))
