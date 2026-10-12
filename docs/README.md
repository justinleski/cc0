# CC0 Knowledge Index

What is written down about this repository, and where. Milestones M0 and M1
have shipped: local infrastructure, a health-check API, and an ingestion worker
that stores explicitly CC0 Met fixtures with rights evidence. Nothing in the
catalog is served to users yet.

M0 acceptance: Compose services, migrations, `/api/v1/health` and
`/api/v1/ready`, and integration checks against pgvector and the RustFS bucket.
M1 acceptance: ten CC0 fixtures ingest repeatably, non-CC0 and unknown records
stay metadata-only, the negative cache prevents refetching, repeats create no
second asset, and failed jobs resume. ADRs are revised in place as decisions
evolve.

## Spec and commands

- [MVP spec](mvp-spec.md)
  Product scope, data model, milestones M0 through M5. Source of truth for work
  not yet built.
- [Local development](local-development.md)
  Startup, shared environment, and database concepts.
- [Evaluation](evaluation.md) — not implemented; spec pointers.
- [AWS deployment](aws-deployment.md) — not implemented; pilot spec pointers.
- [M2 engineering handoff](m2-handoff.md) — pooling and the M1 catalog grants
  are implemented; the grant matrix for M2+ tables is still required.
- [Lint and tests](lint-test.md)
  The individual commands behind `./scripts/check.sh`.

## Systems

- [Storage](systems/storage.md)
  Postgres with pgvector, RustFS, migrations, database roles and catalog grants.
- [API process](systems/api.md)
  FastAPI health and readiness endpoints, configuration, request logging, pools.
- [Ingestion](systems/ingestion.md)
  Met adapter, rights gate, fetch gate, the job row as dead letter, resume
  rules, and operator commands.

## Decisions

- [ADR-0001: S3-compatible object storage](decisions/0001-object-storage.md)
- [ADR-0002: PostgreSQL with pgvector as the single store](decisions/0002-postgres-pgvector.md)
- [ADR-0003: Separate database logins per component](decisions/0003-database-roles.md)
- [ADR-0004: Application processes run on the host](decisions/0004-host-processes.md)

## Not documented yet

Embeddings and search, boards, and exports are milestones M2 through M4 in the
[spec](mvp-spec.md). Each gets a page under `systems/` when its code exists. The
frontend browses a mock catalog in the meantime; see
[frontend/README.md](../frontend/README.md).
