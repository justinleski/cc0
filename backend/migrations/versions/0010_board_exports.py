"""Board exports: owner-scoped requests, a pinned item snapshot, and the export job kind.

Revision ID: 0010_board_exports
Revises: 0008_board_create_idempotency

The API creates an export, its item snapshot, and its job row in one transaction,
so a committed export always has a job to run. To allow that it gets a
column-limited INSERT on `jobs`: no SELECT, UPDATE, or DELETE, and only the
columns that describe new work. The worker reads and settles exports and never
deletes. It still has no access to boards or board items, because the snapshot is
taken when the export is requested.

When the recommendation migration (0009) lands, point `down_revision` at it.
"""

from alembic import op

from app.db import catalog, roles
from app.db.roles import quote_identifier, quote_literal

revision = "0010_board_exports"
down_revision = "0008_board_create_idempotency"
branch_labels = None
depends_on = None


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(quote_literal(value) for value in values)})"


def upgrade() -> None:
    op.drop_constraint("jobs_kind_known", "jobs", type_="check")
    op.create_check_constraint("jobs_kind_known", "jobs", _one_of("kind", catalog.JOB_KINDS))

    op.execute(f"""
        CREATE TABLE {catalog.EXPORTS_TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id uuid NOT NULL REFERENCES users(id),
            board_id uuid REFERENCES boards(id) ON DELETE SET NULL,
            board_name text NOT NULL,
            status text NOT NULL DEFAULT {quote_literal(catalog.EXPORT_QUEUED)},
            failure_reason text,
            artifact_key text,
            artifact_sha256 char(64),
            artifact_bytes bigint,
            included_count integer,
            excluded_count integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            expires_at timestamptz,
            CONSTRAINT board_exports_status_known
                CHECK ({_one_of("status", catalog.EXPORT_STATUSES)}),
            CONSTRAINT board_exports_ready_has_artifact CHECK (
                status NOT IN ({quote_literal(catalog.EXPORT_READY)})
                OR (artifact_key IS NOT NULL AND artifact_sha256 IS NOT NULL
                    AND artifact_bytes IS NOT NULL AND expires_at IS NOT NULL
                    AND included_count > 0)
            ),
            CONSTRAINT board_exports_failed_has_reason CHECK (
                status <> {quote_literal(catalog.EXPORT_FAILED)} OR failure_reason IS NOT NULL
            )
        )
    """)
    op.execute(
        f"CREATE INDEX board_exports_owner_created_idx "
        f"ON {catalog.EXPORTS_TABLE}(owner_user_id, created_at DESC, id)"
    )
    op.execute(
        f"CREATE INDEX board_exports_board_created_idx "
        f"ON {catalog.EXPORTS_TABLE}(board_id, created_at DESC) WHERE board_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX board_exports_expiry_idx ON {catalog.EXPORTS_TABLE}(expires_at) "
        f"WHERE status = {quote_literal(catalog.EXPORT_READY)}"
    )

    op.execute(f"""
        CREATE TABLE {catalog.EXPORT_ITEMS_TABLE} (
            export_id uuid NOT NULL REFERENCES {catalog.EXPORTS_TABLE}(id) ON DELETE CASCADE,
            asset_id uuid NOT NULL REFERENCES assets(id),
            position integer NOT NULL CHECK (position >= 0),
            packaged boolean NOT NULL DEFAULT false,
            excluded_reason text,
            PRIMARY KEY (export_id, asset_id),
            CONSTRAINT board_export_items_position_unique UNIQUE (export_id, position)
        )
    """)
    op.execute(
        f"CREATE INDEX board_export_items_asset_idx ON {catalog.EXPORT_ITEMS_TABLE}(asset_id)"
    )

    api = quote_identifier(roles.ROLE_API)
    worker = quote_identifier(roles.ROLE_WORKER)
    exports = quote_identifier(catalog.EXPORTS_TABLE)
    items = quote_identifier(catalog.EXPORT_ITEMS_TABLE)
    op.execute(f"GRANT SELECT, INSERT ON {exports}, {items} TO {api}")
    op.execute(f"GRANT SELECT, UPDATE ON {exports}, {items} TO {worker}")
    op.execute(f"GRANT INSERT (kind, dedupe_key, payload, max_attempts) ON jobs TO {api}")


def downgrade() -> None:
    api = quote_identifier(roles.ROLE_API)
    op.execute(f"REVOKE INSERT (kind, dedupe_key, payload, max_attempts) ON jobs FROM {api}")
    op.execute(f"DROP TABLE {catalog.EXPORT_ITEMS_TABLE}")
    op.execute(f"DROP TABLE {catalog.EXPORTS_TABLE}")
    op.execute(f"DELETE FROM jobs WHERE kind = {quote_literal(catalog.JOB_KIND_EXPORT)}")
    op.drop_constraint("jobs_kind_known", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_kind_known",
        "jobs",
        _one_of("kind", (catalog.JOB_KIND_INGEST, catalog.JOB_KIND_EMBED)),
    )
