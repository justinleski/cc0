# CC0 Knowledge Index

What is written down about this repository, and where. Milestones M0 through M2
have shipped: local infrastructure, a health-check API, an ingestion worker
that stores explicitly CC0 Met fixtures with rights evidence, and hybrid search
over those assets once an embedding job marks them `READY`. Boards are not
built yet. The default embedding provider is a deterministic fake; a real
OpenCLIP checkpoint is optional and still unpinned.

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
- [M2 engineering handoff](m2-handoff.md) — pooling, catalog grants, and the
  embedding grants are implemented. Grants for users, boards, and exports are
  still required when those tables are created.
- [Lint and tests](lint-test.md)
  The individual commands behind `./scripts/check.sh`.

## Systems

- [Storage](systems/storage.md)
  Postgres with pgvector, RustFS, migrations, database roles and catalog grants.
- [API process](systems/api.md)
  FastAPI health, readiness, and catalog routes, configuration, request logging, pools.
- [Ingestion](systems/ingestion.md)
  Met adapter, rights gate, fetch gate, the job row as dead letter, resume
  rules, and operator commands.
- [Search and embeddings](systems/search.md)
  The embed job, hybrid ranking, quoted phrases, and the public provenance view.

## Decisions

- [ADR-0001: S3-compatible object storage](decisions/0001-object-storage.md)
- [ADR-0002: PostgreSQL with pgvector as the single store](decisions/0002-postgres-pgvector.md)
- [ADR-0003: Separate database logins per component](decisions/0003-database-roles.md)
- [ADR-0004: Application processes run on the host](decisions/0004-host-processes.md)

## Not documented yet

Boards and exports are milestones M3 and M4 in the [spec](mvp-spec.md). Each
gets a page under `systems/` when its code exists. The frontend browses a mock
catalog in the meantime; see [frontend/README.md](../frontend/README.md).
