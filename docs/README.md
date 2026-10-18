# CC0 Knowledge Index

What is written down about this repository, and where. Milestones M0 through M2
and M3 have shipped: local infrastructure, a health-check API, an ingestion worker
that stores explicitly CC0 Met fixtures with rights evidence, and hybrid search
over those assets once an embedding job marks them `READY`. M3 connects the frontend to the catalog and private boards with local authentication. The default embedding provider is a deterministic fake; a real
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
  Container and host startup, shared environment, and database concepts.
- [Evaluation](evaluation.md) — recommendation evaluation and benchmarks.
- [Met evaluation catalog](met-evaluation-catalog.md) — versioned 1,000-image
  dataset, local reproduction, rights audit, and measured distribution.
- [AWS deployment](aws-deployment.md) — not implemented; pilot spec pointers.
- [M2 engineering handoff](m2-handoff.md) — pooling, catalog grants, and the
  embedding grants are implemented. Users and boards grants are implemented; exports remain future work.
- [Lint and tests](lint-test.md)
  The individual commands behind `./scripts/check.sh`.

## Systems

- [Catalog frontend](systems/catalog-frontend.md)
  M3 slice 1: real browse/search, provenance, related images, and gated media.

- [Private boards and local authentication](systems/boards.md)
  M3 slices 2 and 3: signed local tokens, owner-scoped board API, and persistent frontend workflows.
- [Board recommendations](systems/recommendations.md)
  M4: centroid baseline on pgvector, retriever/ranker split, experimental
  complement control, stale-response-safe frontend rail.
- [Storage](systems/storage.md)
  Postgres with pgvector, RustFS, migrations, database roles and catalog grants.
- [API process](systems/api.md)
  FastAPI health, readiness, and catalog routes, configuration, request logging, pools.
- [Ingestion](systems/ingestion.md)
  Met adapter, rights gate, fetch gate, the job row as dead letter, resume
  rules, operator commands, feature-local layers, transaction boundaries, and
  completed refactor validation results.
- [Search and embeddings](systems/search.md)
  The embed job, hybrid ranking, quoted phrases, public provenance view, and
  a code-reading guide to the feature-local service/repository/query layers.

## Decisions

- [ADR-0001: S3-compatible object storage](decisions/0001-object-storage.md)
- [ADR-0002: PostgreSQL with pgvector as the single store](decisions/0002-postgres-pgvector.md)
- [ADR-0003: Separate database logins per component](decisions/0003-database-roles.md)
- [ADR-0004: Local host and container workflows](decisions/0004-host-processes.md)
- [ADR-0005: Feature-based layered architecture](decisions/0005-feature-based-layered-architecture.md)

## Not documented yet

Exports remain future work in the [spec](mvp-spec.md). Board recommendations
have a baseline; complement ranking is still a research track (see
[recommendations](systems/recommendations.md#complement-is-an-open-problem)).
The frontend browses the real catalog and saves private boards through the API.
See [frontend/README.md](../frontend/README.md).
