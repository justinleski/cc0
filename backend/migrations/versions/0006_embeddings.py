"""Embeddings, search lexemes, and the public provenance view.

Revision ID: 0006_embeddings
Revises: 0005_claim_token
Create Date: 2026-10-14

`asset_embeddings` is insert-only for the worker: a new model version is a new
row, so search can point back at an older checkpoint without rewriting vectors.
The API selects those rows and the `ready_assets` view. It still cannot read
`source_records` or `rights_evidence`; the view is owned by the migration role
and exposes a READY asset only when its latest evidence row is verified CC0.
The latest row is chosen first (`checked_at DESC, id DESC`); the CC0 predicate
applies to that row.

Downgrade deletes `kind = embed` jobs before it restores the ingest-only check.
Those jobs name a checkpoint and a vector table this revision removes. Ingest
jobs stay.

Lexemes live on `assets` because a full-text index cannot join `creators`.
Names and aliases stay unstemmed. Titles, tags, and descriptions are also
stored stemmed, so a tag can match a plural without turning a name like
Running into run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import catalog, roles
from app.db.roles import quote_identifier, quote_literal

revision: str = "0006_embeddings"
down_revision: str | None = "0005_claim_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FILL_LEXEMES = """
CREATE FUNCTION cc0_fill_asset_lexemes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    creator_name text := '';
    creator_aliases text := '';
    tags text;
BEGIN
    IF NEW.creator_id IS NOT NULL THEN
        SELECT creators.name,
               CASE
                   WHEN jsonb_typeof(creators.aliases) = 'array' THEN coalesce((
                       SELECT string_agg(value, ' ')
                       FROM jsonb_array_elements_text(creators.aliases) AS value
                   ), '')
                   ELSE ''
               END
          INTO creator_name, creator_aliases
          FROM creators
         WHERE creators.id = NEW.creator_id;
    END IF;
    tags := coalesce(array_to_string(NEW.style_tags, ' '), '');
    NEW.lexeme_simple :=
        setweight(to_tsvector('simple', coalesce(creator_name, '')), 'A')
        || setweight(to_tsvector('simple', coalesce(creator_aliases, '')), 'A')
        || setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'B')
        || setweight(to_tsvector('simple', tags), 'B')
        || setweight(to_tsvector('simple', coalesce(NEW.description, '')), 'C');
    NEW.lexeme_english :=
        setweight(to_tsvector('english', coalesce(NEW.title, '')), 'B')
        || setweight(to_tsvector('english', tags), 'B')
        || setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C');
    RETURN NEW;
END;
$$;
"""

_REFRESH_CREATOR = """
CREATE FUNCTION cc0_refresh_creator_lexemes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE assets SET creator_id = creator_id WHERE creator_id = NEW.id;
    RETURN NEW;
END;
$$;
"""


def _one_of(column: str, values: Sequence[str]) -> str:
    allowed = ", ".join(quote_literal(value) for value in values)
    return f"{column} IN ({allowed})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Databases migrated before embed existed have a check that names only
    # ingest. A fresh install already includes embed, because 0003 reads the
    # live catalog; replacing the check makes both cases the same.
    op.drop_constraint("jobs_kind_known", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_kind_known",
        "jobs",
        _one_of("kind", catalog.JOB_KINDS),
    )

    op.add_column("assets", sa.Column("lexeme_simple", postgresql.TSVECTOR(), nullable=True))
    op.add_column("assets", sa.Column("lexeme_english", postgresql.TSVECTOR(), nullable=True))
    op.execute(_FILL_LEXEMES)
    op.execute(_REFRESH_CREATOR)
    op.execute(
        """
        CREATE TRIGGER assets_fill_lexemes
        BEFORE INSERT OR UPDATE ON assets
        FOR EACH ROW EXECUTE FUNCTION cc0_fill_asset_lexemes()
        """
    )
    op.execute(
        """
        CREATE TRIGGER creators_refresh_lexemes
        AFTER UPDATE OF name, aliases ON creators
        FOR EACH ROW EXECUTE FUNCTION cc0_refresh_creator_lexemes()
        """
    )
    op.execute("UPDATE assets SET title = title")
    op.alter_column("assets", "lexeme_simple", nullable=False)
    op.alter_column("assets", "lexeme_english", nullable=False)
    op.create_index(
        "assets_lexeme_simple_idx",
        "assets",
        ["lexeme_simple"],
        postgresql_using="gin",
    )
    op.create_index(
        "assets_lexeme_english_idx",
        "assets",
        ["lexeme_english"],
        postgresql_using="gin",
    )
    op.execute("CREATE INDEX creators_name_trgm_idx ON creators USING gin (name gin_trgm_ops)")

    dimensions = catalog.VECTOR_DIMENSIONS
    op.execute(
        f"""
        CREATE TABLE {catalog.EMBEDDINGS_TABLE} (
            asset_id uuid NOT NULL REFERENCES assets (id) ON DELETE RESTRICT,
            model_key text NOT NULL,
            model_version text NOT NULL,
            dimensions integer NOT NULL,
            embedding vector({dimensions}) NOT NULL,
            generated_at timestamp with time zone NOT NULL DEFAULT now(),
            PRIMARY KEY (asset_id, model_key, model_version),
            CONSTRAINT asset_embeddings_dimensions_match
                CHECK (dimensions = {dimensions}),
            CONSTRAINT asset_embeddings_model_key_present
                CHECK (char_length(model_key) > 0),
            CONSTRAINT asset_embeddings_model_version_present
                CHECK (char_length(model_version) > 0)
        )
        """
    )

    ready = quote_literal(catalog.ASSET_READY)
    cc0 = quote_literal(catalog.RIGHTS_CC0)
    verified = quote_literal(catalog.VERIFICATION_VERIFIED)
    # security_invoker false: the view owner (the migration superuser) reads
    # source_records and rights_evidence. cc0_api is granted the view only.
    # The lateral subquery picks the latest evidence row, then the outer WHERE
    # keeps the asset when that row is verified CC0 and the asset is READY.
    op.execute(
        f"""
        CREATE VIEW {catalog.READY_ASSETS_VIEW}
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
        """
    )

    worker = quote_identifier(roles.ROLE_WORKER)
    api = quote_identifier(roles.ROLE_API)
    embeddings = quote_identifier(catalog.EMBEDDINGS_TABLE)
    view = quote_identifier(catalog.READY_ASSETS_VIEW)
    op.execute(f"GRANT SELECT, INSERT ON {embeddings} TO {worker}")
    op.execute(f"GRANT SELECT ON {embeddings} TO {api}")
    op.execute(f"GRANT SELECT ON {view} TO {api}")


def downgrade() -> None:
    worker = quote_identifier(roles.ROLE_WORKER)
    api = quote_identifier(roles.ROLE_API)
    embeddings = quote_identifier(catalog.EMBEDDINGS_TABLE)
    view = quote_identifier(catalog.READY_ASSETS_VIEW)
    op.execute(f"REVOKE ALL ON {view} FROM {api}")
    op.execute(f"REVOKE ALL ON {embeddings} FROM {worker}, {api}")
    op.execute(f"DROP VIEW {view}")
    op.execute(f"DROP TABLE {embeddings}")
    op.execute("DROP INDEX creators_name_trgm_idx")
    op.drop_index("assets_lexeme_english_idx", table_name="assets")
    op.drop_index("assets_lexeme_simple_idx", table_name="assets")
    op.execute("DROP TRIGGER creators_refresh_lexemes ON creators")
    op.execute("DROP TRIGGER assets_fill_lexemes ON assets")
    op.execute("DROP FUNCTION cc0_refresh_creator_lexemes()")
    op.execute("DROP FUNCTION cc0_fill_asset_lexemes()")
    op.drop_column("assets", "lexeme_english")
    op.drop_column("assets", "lexeme_simple")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    # Embed rows have no meaning once the vector table and the embed kind are
    # gone. Delete them before the ingest-only check is validated, including
    # succeeded jobs. Ingest jobs stay.
    op.execute(
        f"DELETE FROM jobs WHERE kind = {quote_literal(catalog.JOB_KIND_EMBED)}"
    )
    op.drop_constraint("jobs_kind_known", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_kind_known",
        "jobs",
        _one_of("kind", (catalog.JOB_KIND_INGEST,)),
    )
