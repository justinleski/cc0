# CC0 Knowledge Index

What is written down about this repository, and where. Milestone M0 is the only
milestone that has shipped, so the system pages below describe local
infrastructure and a health-check API, not a catalog.

## Spec and commands

- [MVP spec](mvp-spec.md)
  Product scope, data model, milestones M0 through M5. Source of truth for work
  not yet built.
- [Lint and tests](lint-test.md)
  The individual commands behind `./scripts/check.sh`.

## Systems

- [Storage](systems/storage.md)
  Postgres with pgvector, RustFS, the first migration, database roles.
- [API process](systems/api.md)
  FastAPI health and readiness endpoints, configuration, request logging.

## Decisions

- [ADR-0001: S3-compatible object storage](decisions/0001-object-storage.md)
- [ADR-0002: PostgreSQL with pgvector as the single store](decisions/0002-postgres-pgvector.md)
- [ADR-0003: Separate database logins per component](decisions/0003-database-roles.md)
- [ADR-0004: Application processes run on the host](decisions/0004-host-processes.md)

## Not documented yet

Ingestion, search, boards, and exports are milestones M1 through M4 in the
[spec](mvp-spec.md). Each gets a page under `systems/` when its code exists. The
frontend browses a mock catalog in the meantime; see
[frontend/README.md](../frontend/README.md).
