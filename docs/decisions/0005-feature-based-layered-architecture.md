# ADR-0005: Feature-Based Layered Architecture

Status: Accepted

## Context

At `f25e508`, search already had a feature package, but `SearchService`
combined orchestration, SQL construction, connection management, row mapping,
and result dataclasses. Its HTTP routes lived separately under `app/api/`.
Testing orchestration in isolation required separating it from database work.

Commit `b619250` extracted shared dataclasses into `search/models.py` and
persistence into `search/repository.py`. The service received a
`SearchRepository` protocol instead of an engine, routes supplied a
`PostgresSearchRepository`, and new service tests used an in-memory repository.
Parsing, embedding calls, rank fusion, pagination, and suggestions remained
service responsibilities.

The subsequent working-tree refinements move the router into `search/`,
separate pure SQL construction into `queries.py`, move cursor validation
helpers into `cursor.py`, and replace the protocol and implementation pair
with a concrete `SearchRepository`. This decision adopts that refined layout
as the backend convention. These refinements are not part of `b619250`.

## Decision

Organize backend application code by feature, with layers inside each feature
package. Keep a feature's HTTP boundary, orchestration, and persistence close
together rather than creating application-wide router, service, and repository
directories. These are module boundaries within the existing application;
they do not introduce separate deployments.

[Search](../systems/search.md#feature-based-layered-modular-monolith) is the
reference implementation:

| Module | Responsibility |
|---|---|
| `router.py` | HTTP parameter validation, response models, serialization, error translation, and wiring service dependencies. |
| `service.py` | Application validation and orchestration: parsing, embeddings, fusion, pagination decisions, and suggestions. |
| `repository.py` | Connection scopes, query execution, and mapping database rows to typed results. |
| `queries.py` | SQL constants and pure builders returning SQL with bound parameters; no database I/O. |
| `models.py` | Shared typed inputs and results without HTTP or database driver types. |
| `parse.py`, `rank.py`, `cursor.py` | Focused parsing, ranking, and cursor helpers used by the feature. |

The request path is router → service → repository. The repository uses query
builders; models and focused helpers support the relevant layers. Services do
not execute SQL or handle HTTP responses, and repositories do not call back
into services or routers. Across the service/repository boundary, pass typed
filters, positions, and results rather than SQL fragments or driver rows.
User values remain bound parameters when queries are executed.

Dependencies are supplied through constructors. In search, the router builds
the concrete repository from the existing application engine and supplies it
and the process's embedding provider to the service. The application entry
point registers the feature router and owns shared resource lifecycles. Shared
configuration, engines, and external integrations retain their existing homes;
feature organization does not require duplicating infrastructure.

Use concrete repositories where they suffice. A protocol, generic repository
framework, or Unit of Work is not required by this convention. Likewise, the
search file list is a reference, not a mandatory scaffold: create layers and
helpers when they separate meaningful responsibilities. Keep PostgreSQL-specific
SQL explicit in Python alongside its parameter builders.

Apply this convention to new backend features. Migrate existing features
incrementally when substantive work warrants it, preserving their behavior.
Ingestion and worker stores retain their current boundaries until such work
occurs; this decision does not claim they already match search. Frontend
organization is outside this decision.

For search, preserve the existing HTTP routes, response shapes, error behavior,
cursor formats, ranking semantics, and database grants. Repository construction
must not check out a connection: text inference finishes before candidate
retrieval opens one. Asset reads continue through `ready_assets`; snapshot card
retrieval rechecks visibility and preserves requested order while omitting hidden
assets. The detailed invariants remain in the
[search system documentation](../systems/search.md#how-to-observe-the-patterns-and-invariants).

## Consequences

+ Related code can be reviewed and changed within one feature package, while
  layer responsibilities make database and HTTP boundaries explicit.
+ Service behavior can be tested with an in-memory repository. Pure query
  builders can be checked without a database, and repository tests can observe
  connection ordering and cleanup. PostgreSQL integration tests remain necessary
  for executed SQL and public visibility; see [verification](../systems/search.md#verification).
+ Existing API contracts and shared infrastructure can stay stable while
  features adopt the layout incrementally.
- More modules and result types add navigation and mapping work. Small features
  should not acquire empty layers solely to resemble search.
- Boundaries require review discipline. A concrete repository keeps the design
  simple but does not provide a formal persistence interface or make the SQL
  portable to another database.
- Incremental adoption leaves different layouts in the backend temporarily;
  system documentation must describe each feature's actual implementation.
