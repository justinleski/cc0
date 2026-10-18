"""Board exports: owner-scoped requests, a pinned item snapshot, and the export job kind.

Revision ID: 0010_board_exports
Revises: 0009_ready_assets_fail_closed

The API creates an export, its item snapshot, and its job row in one transaction,
so a committed export always has a job to run. The API role has no privilege on
`jobs`; it may only EXECUTE `cc0_enqueue_export_job`, a SECURITY DEFINER function
that inserts the one job an existing QUEUED export needs. The worker reads and
settles exports and never deletes. It still has no access to boards or board
items, because the snapshot is taken when the export is requested.
"""

from alembic import op

from app.db import catalog, roles
from app.db.roles import quote_identifier, quote_literal

revision = "0010_board_exports"
down_revision = "0009_ready_assets_fail_closed"
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
    op.execute("""
        CREATE FUNCTION cc0_enqueue_export_job(p_export_id uuid, p_max_attempts integer)
        RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            INSERT INTO jobs (kind, dedupe_key, payload, max_attempts)
            SELECT 'export', 'export:' || id::text,
                   jsonb_build_object('export_id', id::text), p_max_attempts
            FROM board_exports WHERE id = p_export_id AND status = 'QUEUED'
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION cc0_enqueue_export_job(uuid, integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION cc0_enqueue_export_job(uuid, integer) TO {api}")


def downgrade() -> None:
    op.execute("DROP FUNCTION cc0_enqueue_export_job(uuid, integer)")
    op.execute(f"DROP TABLE {catalog.EXPORT_ITEMS_TABLE}")
    op.execute(f"DROP TABLE {catalog.EXPORTS_TABLE}")
    op.execute(f"DELETE FROM jobs WHERE kind = {quote_literal(catalog.JOB_KIND_EXPORT)}")
    op.drop_constraint("jobs_kind_known", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_kind_known",
        "jobs",
        _one_of("kind", (catalog.JOB_KIND_INGEST, catalog.JOB_KIND_EMBED)),
    )
