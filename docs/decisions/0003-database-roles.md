# ADR-0003: Separate Database Logins per Component

Status: Accepted

## Context

Running the API, the ingestion worker, and migrations as one superuser is the
easy default, and it makes every later privilege question harder: an API bug can
drop a table, and nothing proves the application only needs the access it claims.
Retrofitting least privilege after the catalog schema exists means auditing every
existing grant.

## Decision

Create three login roles in the very first migration, before any catalog table
exists: `cc0_migrator`, `cc0_api`, and `cc0_worker`. All three are
`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`. `cc0_migrator` gets
`USAGE, CREATE` on schema `public`; `cc0_api` and `cc0_worker` get `USAGE` only.
Alembic itself still connects as the superuser through a separate
`MIGRATION_DATABASE_URL`, because creating roles and extensions requires
superuser. The API readiness check connects as `cc0_api`.

## Consequences

+ Table grants can be decided per role as the schema is written, instead of
  being reconstructed later.
+ Readiness exercises the real application login, so a privilege mistake shows
  up as a failed check rather than as a runtime error in production.
+ An integration test can assert that `cc0_api` and `cc0_worker` cannot
  `CREATE TABLE`.
- There are four credentials to configure and keep in sync instead of one.
- Roles are cluster-wide, not per-database, so tests that exercise the migration
  must use suffixed role names to avoid clobbering the real logins.
- The migration builds role SQL as a dollar-quoted `DO` block using string
  interpolation, because a password can contain a character SQLAlchemy would
  read as a bind parameter. That requires explicit identifier and password
  validation in `backend/app/db/roles.py`.
