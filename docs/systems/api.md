# API process

Status: Implemented (health, readiness, and catalog search)
Owner: [backend/app/](../../backend/app/main.py)
Depends on: PostgreSQL, RustFS
Related: [storage.md](storage.md), [search.md](search.md), [ADR-0003](../decisions/0003-database-roles.md), [ADR-0004](../decisions/0004-host-processes.md)

## Summary

A FastAPI application under `/api/v1`: liveness, readiness, and the public
catalog. Search, asset detail, and related images read `ready_assets` (READY,
and the latest evidence row is verified CC0). Creator lookup reads `creators`.
Board routes are not built. Middleware attaches a request id to
every response and logs one line per request. Query strings are not logged.

## Architecture

```mermaid
flowchart TB
  CLIENT["curl / TestClient"] --> MW["RequestContextMiddleware assigns X-Request-ID"]
  MW --> HEALTH["GET /api/v1/health"]
  MW --> READY["GET /api/v1/ready"]
  MW --> SEARCH["GET /api/v1/assets, /assets/id, /related, /creators"]
  HEALTH -->|"200 always"| OUT200["status: ok"]
  READY --> RUN["run_checks"]
  RUN --> CPG["check_postgres: SELECT 1 as cc0_api"]
  RUN --> COBJ["check_object_store: HeadBucket cc0"]
  CPG --> PG[("PostgreSQL")]
  COBJ --> FS["RustFS"]
  RUN -->|"all ok"| OUT["200"]
  RUN -->|"DependencyUnavailable"| OUT503["503, both checks"]
  RUN -->|"unexpected error"| OUT500["500"]
```

## Important invariants

- `GET /api/v1/health` returns `200 {"status": "ok"}` whenever the process is
  up. It opens no connections, so a load balancer cannot confuse a degraded
  dependency with a dead process.
- `GET /api/v1/ready` is `200` only when both checks pass, and `503` when an
  operational `DependencyUnavailable` is reported. Unexpected errors abort the
  route with `500`. A 503 body always carries both keys: `{"status":
  "unavailable", "checks": {"postgres": "ok"|"error", "object_store":
  "ok"|"error"}}`.
- The "every check still runs" rule applies to operational
  `DependencyUnavailable` failures, so one 503 shows which dependencies are
  down. An unexpected error does not continue the remaining checks.
- The Postgres check connects as `cc0_api` through `DATABASE_URL`, not as the
  superuser. Readiness therefore exercises the privileges the API really has.
- The object store check is a path-style `HeadBucket` on the configured bucket,
  signed with `s3v4` and `max_attempts: 1`, so the endpoint, credentials, and
  bucket name are all verified without retry delay.
- Responses expose no credentials, URLs, or driver text. `run_checks` collapses
  operational failures to the string `error`, and even in logs the detail is reduced to
  the underlying exception class name; exception messages are never used.
- Every response carries `X-Request-ID`. An inbound `x-request-id` header is
  reused; otherwise a UUID4 is generated.
- Request logs record method, path, status, and request id. Query strings,
  headers, tokens, and signed URLs are never logged, and botocore, boto3, and
  urllib3 are pinned to `WARNING` because botocore logs signed request details
  at `DEBUG`.
- Settings come from the process environment or repository-root `.env`.
  Unknown `.env` keys fail except the Compose allowlist. A process-environment
  name is rejected when, case-insensitively, it is not a Settings field and not
  on that allowlist, and not the ignored ambient name `DATABASE_PASSWORD`, and
  it starts with `APP_`, `DATABASE`, `MIGRATION_`, `WORKER_`, `CC0_`,
  `OBJECT_STORE_`, `READINESS_`, `INGEST_`, `EMBEDDING_`, `MODEL_`, `POSTGRES_`,
  `RUSTFS_`, `S3_`, or `API_`.
  `PATH`, `HOME`, and `GITHUB_*` stay allowed. Database logins are the three
  URLs plus `CC0_*_PASSWORD`; `DATABASE_PASSWORD` is not read. `APP_ENV`
  other than `local` (case-insensitive, whitespace stripped) rejects dummy
  secrets by exact match, database URLs with an empty or missing password, and
  an object-store host of `localhost` or `127.0.0.1`. See
  [local development](../local-development.md).
- Uvicorn loads `app.main:app` at import and fails fast on a bad `.env` or on
  dummy secrets when `APP_ENV` is not local. The unit suite imports `create_app`
  under pytest, which skips that module-level app, and passes `_env_file=None`
  so collection does not read the repository `.env`.
- Process entry points load settings once; requests use `app.state.settings`,
  never `get_settings()`. Tests inject `create_app(Settings(...))`.
- Lifespan creates the API pool from
  [app/db/engine.py](../../backend/app/db/engine.py), one S3 client, and the
  configured embedding provider (`prepare()` loads OpenCLIP only when that
  provider is selected), then disposes and closes them at shutdown. Endpoints
  and readiness share that one engine on `app.state`: pool size 5, overflow 5,
  `pool_pre_ping`, 30 minute recycle, 10 second checkout timeout, 5 second
  psycopg `connect_timeout`. The ingestion worker builds its own 2 + 2 pool in
  its own process and never imports this one ([ingestion](ingestion.md)).
  Search routes and ranking rules are in [search](search.md). Catalog reads go
  through `ready_assets`. Query text is encoded before a connection is checked
  out of this pool, and each process runs one encode at a time, so inference
  does not occupy a pooled connection.
- Catalog routes construct `SearchService` with a `SearchRepository`
  backed by that same engine and the process's embedding provider. The repository
  owns connections, SQL execution, and row mapping; `app/search/queries.py`
  owns SQL construction. The service owns query parsing, embedding calls,
  fusion, cursors, and suggestion decisions. Their shared inputs and results are
  typed data classes in `app/search/models.py`. HTTP models and response formats
  remain in `app/search/router.py`.
- `statement_timeout` is `SET LOCAL` inside the probe transaction, not an
  engine-wide connection option, so a later request on the same connection
  does not inherit the readiness budget.
- `READINESS_TIMEOUT_SECONDS` defaults to 2 seconds and bounds the probe
  statement and S3 connect/read. Connect and pool checkout use the shared
  pool's own limits, so an unreachable Postgres takes up to the 5 second
  `connect_timeout` to report. Checks run sequentially, so two hung
  dependencies add up.

## Failure handling

- A dependency failure is a `503` with structured per-check results, not an
  exception reaching the client. `DependencyUnavailable` is raised inside each
  check and caught by `run_checks`.
- `run_checks` catches `DependencyUnavailable` and continues. It logs the cause
  class (for example `OperationalError`), not `DependencyUnavailable`, and does
  not log `str(exc)` or `exc_info`. Unexpected exceptions are logged as request
  id, check name, and exception class, then re-raised as an error whose message
  is only that class name, so the middleware traceback has no driver text, URL,
  or secret. Those become 500s. The request id is still on both log lines.
- An unhandled exception outside readiness is logged with the request id and
  re-raised, so the traceback is correlatable without altering the response
  contract.
- Timeouts bound every readiness probe, so an unreachable host fails fast
  instead of hanging the check.
- The integration test asserts the full ready body against a live stack rather
  than only the status code, which catches a check silently reporting `ok` while
  doing nothing.

## Design decisions

- Backend features keep their HTTP, service, and persistence layers together,
  with search as the reference implementation:
  [ADR-0005](../decisions/0005-feature-based-layered-architecture.md).
- The API, the worker, and the frontend run on the host against Compose
  data services, and are not containerized yet: [ADR-0004](../decisions/0004-host-processes.md).
- Readiness uses the restricted `cc0_api` login so the check fails when
  privileges are wrong: [ADR-0003](../decisions/0003-database-roles.md).
- The object store is reached through the S3 API so the same code path works
  against RustFS locally and S3 later: [ADR-0001](../decisions/0001-object-storage.md).
