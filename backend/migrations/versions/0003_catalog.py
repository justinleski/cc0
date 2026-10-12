"""Create the ingestion catalog and replace the M0 default privileges.

Revision ID: 0003_catalog
Revises: 0002_defaults
Create Date: 2026-10-12

Six tables only: providers, creators, source_records, rights_evidence, assets,
and jobs. Embeddings, boards, exports, and users belong to later milestones.

The broad default privileges from 0002 are revoked before the first CREATE
TABLE, otherwise every table here would inherit SELECT/INSERT/UPDATE/DELETE for
both application roles. Grants are then applied per table: the worker writes the
catalog and never deletes, and the API reads only creators and assets. Every
primary key is a UUID, so this revision creates no sequence to grant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import catalog, roles
from app.db.roles import default_privilege_statements, quote_identifier, quote_literal

revision: str = "0003_catalog"
down_revision: str | None = "0002_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table privileges, read at call time like the role names. The worker has no
# DELETE anywhere: a dead job and its rights evidence must survive for an
# operator to inspect. The API cannot see providers, source records, evidence,
# or jobs at all, so an API bug cannot rewrite a rights decision.
WORKER_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE"
API_READABLE_TABLES = ("creators", "assets")

_TIMESTAMP = postgresql.TIMESTAMP(timezone=True)
_NOW = sa.text("now()")
_NEW_UUID = sa.text("gen_random_uuid()")


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=_NEW_UUID,
    )


def _one_of(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(quote_literal(value) for value in values)
    return f"{column} IN ({allowed})"


def _grant_tables() -> None:
    worker = quote_identifier(roles.ROLE_WORKER)
    api = quote_identifier(roles.ROLE_API)
    for table in catalog.TABLES:
        op.execute(f"GRANT {WORKER_TABLE_PRIVILEGES} ON {quote_identifier(table)} TO {worker}")
    for table in API_READABLE_TABLES:
        op.execute(f"GRANT SELECT ON {quote_identifier(table)} TO {api}")


def _revoke_tables() -> None:
    worker = quote_identifier(roles.ROLE_WORKER)
    api = quote_identifier(roles.ROLE_API)
    for table in catalog.TABLES:
        op.execute(f"REVOKE ALL ON {quote_identifier(table)} FROM {worker}, {api}")


def upgrade() -> None:
    for statement in default_privilege_statements("REVOKE", "FROM"):
        op.execute(statement)

    op.create_table(
        "providers",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Requests per second the operator allows us, not the provider's maximum.
        sa.Column(
            "rate_limit_rps",
            sa.Numeric(6, 3),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        # The published policy that makes an item flag mean CC0. Recorded as
        # evidence on every rights decision, so it cannot be null.
        sa.Column("policy_url", sa.Text(), nullable=False),
        sa.Column("last_cursor", sa.Text(), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("name", name="providers_name_key"),
        sa.CheckConstraint("rate_limit_rps > 0", name="providers_rate_limit_positive"),
    )

    op.create_table(
        "creators",
        _uuid_pk(),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Provider-scoped identity: the artist's authority URL when the item has
        # one, otherwise a name key. Normalizing people across providers is not
        # a rights-safe operation and is not attempted.
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("provider_id", "external_key", name="creators_provider_external_key"),
    )

    op.create_table(
        "source_records",
        _uuid_pk(),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("providers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("landing_url", sa.Text(), nullable=True),
        # A URL string only. Image bytes for a non-eligible record are never
        # fetched, so this column is a pointer, not a promise of storage.
        sa.Column("original_url", sa.Text(), nullable=True),
        # Allowlisted provider fields only. No data URIs or embedded previews.
        sa.Column(
            "raw_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "rights_classification",
            sa.Text(),
            nullable=False,
            server_default=catalog.RIGHTS_UNKNOWN,
        ),
        sa.Column(
            "eligibility_status",
            sa.Text(),
            nullable=False,
            server_default=catalog.ELIGIBILITY_DISCOVERED,
        ),
        sa.Column("last_rights_checked_at", _TIMESTAMP, nullable=True),
        sa.Column("next_recheck_at", _TIMESTAMP, nullable=True),
        sa.Column("first_seen_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("last_fetched_at", _TIMESTAMP, nullable=True),
        sa.UniqueConstraint("provider_id", "external_id", name="source_records_provider_external"),
        sa.CheckConstraint(
            _one_of("rights_classification", catalog.RIGHTS_CLASSIFICATIONS),
            name="source_records_rights_classification_known",
        ),
        sa.CheckConstraint(
            _one_of("eligibility_status", catalog.ELIGIBILITY_STATUSES),
            name="source_records_eligibility_known",
        ),
    )
    op.create_index("source_records_next_recheck_at_idx", "source_records", ["next_recheck_at"])
    op.create_index("source_records_eligibility_idx", "source_records", ["eligibility_status"])

    op.create_table(
        "assets",
        _uuid_pk(),
        sa.Column(
            "source_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creators.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location_label", sa.Text(), nullable=True),
        sa.Column("created_year_start", sa.Integer(), nullable=True),
        sa.Column("created_year_end", sa.Integer(), nullable=True),
        sa.Column(
            "style_tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("original_key", sa.Text(), nullable=False),
        sa.Column("thumbnail_key", sa.Text(), nullable=False),
        # Not unique on purpose. Two source records may hold the same bytes and
        # still deserve separate rows, each with its own rights history. A
        # cross-source "same artwork" claim is a relation, not a merge.
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("ingested_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("status", sa.Text(), nullable=False),
        sa.UniqueConstraint("source_record_id", name="assets_source_record_key"),
        sa.CheckConstraint(_one_of("status", catalog.ASSET_STATUSES), name="assets_status_known"),
        sa.CheckConstraint("width > 0 AND height > 0", name="assets_dimensions_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="assets_sha256_lowercase_hex"),
        sa.CheckConstraint(
            "created_year_start IS NULL"
            " OR created_year_end IS NULL"
            " OR created_year_start <= created_year_end",
            name="assets_year_range_ordered",
        ),
    )
    op.create_index("assets_status_idx", "assets", ["status"])
    op.create_index("assets_creator_idx", "assets", ["creator_id"])
    op.create_index("assets_sha256_idx", "assets", ["sha256"])

    op.create_table(
        "rights_evidence",
        _uuid_pk(),
        # Evidence attaches to the source record even when no asset is ever
        # created, which is the whole point of the negative cache.
        sa.Column(
            "source_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("rights_type", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("raw_statement", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification_method", sa.Text(), nullable=False),
        sa.Column("checked_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        # No users table yet, so this stays an unconstrained UUID until M3.
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            _one_of("rights_type", catalog.RIGHTS_CLASSIFICATIONS),
            name="rights_evidence_rights_type_known",
        ),
        sa.CheckConstraint(
            _one_of("verification_status", catalog.VERIFICATION_STATUSES),
            name="rights_evidence_verification_status_known",
        ),
        sa.CheckConstraint(
            _one_of("verification_method", catalog.VERIFICATION_METHODS),
            name="rights_evidence_method_known",
        ),
    )
    op.create_index(
        "rights_evidence_source_checked_idx",
        "rights_evidence",
        ["source_record_id", sa.text("checked_at DESC")],
    )

    op.create_table(
        "jobs",
        _uuid_pk(),
        sa.Column("kind", sa.Text(), nullable=False),
        # Stable identity for one unit of work. A crash, a duplicate delivery,
        # or a second ingest run reuses this row instead of downloading twice.
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=catalog.JOB_PENDING),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("next_run_at", _TIMESTAMP, nullable=True),
        # Single-flight claim. An expired lease is reclaimable, and that reclaim
        # counts as an attempt so a crash loop cannot run forever.
        sa.Column("leased_until", _TIMESTAMP, nullable=True),
        sa.Column("dead_at", _TIMESTAMP, nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("dedupe_key", name="jobs_dedupe_key_key"),
        sa.CheckConstraint(_one_of("kind", catalog.JOB_KINDS), name="jobs_kind_known"),
        sa.CheckConstraint(_one_of("state", catalog.JOB_STATES), name="jobs_state_known"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="jobs_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            f"state <> {quote_literal(catalog.JOB_DEAD)} OR dead_at IS NOT NULL",
            name="jobs_dead_has_timestamp",
        ),
        sa.CheckConstraint(
            f"state <> {quote_literal(catalog.JOB_LEASED)} OR leased_until IS NOT NULL",
            name="jobs_leased_has_deadline",
        ),
    )
    op.create_index("jobs_claim_idx", "jobs", ["kind", "state", "next_run_at"])

    _grant_tables()


def downgrade() -> None:
    _revoke_tables()
    op.drop_index("jobs_claim_idx", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("rights_evidence_source_checked_idx", table_name="rights_evidence")
    op.drop_table("rights_evidence")
    for index in ("assets_sha256_idx", "assets_creator_idx", "assets_status_idx"):
        op.drop_index(index, table_name="assets")
    op.drop_table("assets")
    for index in ("source_records_eligibility_idx", "source_records_next_recheck_at_idx"):
        op.drop_index(index, table_name="source_records")
    op.drop_table("source_records")
    op.drop_table("creators")
    op.drop_table("providers")
    # Leave the database as 0002 left it, so `downgrade base` still runs.
    for statement in default_privilege_statements("GRANT", "TO"):
        op.execute(statement)
