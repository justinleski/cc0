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

## Default grants and password ownership

Revision 0002 grants SELECT/INSERT/UPDATE/DELETE on future tables and USAGE/SELECT
on future sequences to API and worker. PostgreSQL default privileges belong to
the object creator: defaults cover both the Alembic superuser and
`cc0_migrator`. They do not change existing objects, and downgrade revokes the
defaults without revoking grants already applied to existing tables. The table
privileges are intentionally broad for M0: API and worker can both modify rows.
The [M2 handoff](../m2-handoff.md) is the required replacement: revoke those
defaults in the migration that creates the catalog, then grant per table.
That replacement is not in the schema yet.

Re-executing 0001 while a role already exists preserves the password and still
reapplies role attributes. The password is not updated, so readiness can fail
until the operator aligns it. `alembic downgrade base` drops the roles, and a
later upgrade creates them again with the password from the environment.
Changing `.env` alone does not rotate an existing cluster. Local rotation is a
new volume, a manual `ALTER ROLE`, or downgrade to base plus upgrade.
Production rotation belongs to operations. See the
[local development database primer](../local-development.md#database-primer)
for cluster-wide roles, grants, and volume lifetime.
