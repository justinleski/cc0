# M2 engineering handoff

Status: Pooling, the environment rule, the M1 catalog grants, and the
`asset_embeddings` grants are implemented. `asset_relations` is not created.
Grants for `users`, `boards`, `board_items`, and `exports` are still required
when those tables are created.

For what is running, see [storage](systems/storage.md),
[ingestion](systems/ingestion.md), and [search](systems/search.md).

## 1. Database connection pooling

Implemented in M1 in [backend/app/db/engine.py](../backend/app/db/engine.py).
Endpoints and `GET /api/v1/ready` share one engine on `app.state` for the API
process, disposed in the lifespan shutdown. The rules below still apply to any
new process or pool.

Background workers run in a separate process ([ADR-0004](decisions/0004-host-processes.md)).
A pool cannot cross that boundary. The worker creates its own engine from
`WORKER_DATABASE_URL` and disposes it when that process exits. It must not
import or borrow the API engine.

Suggested local budget, per process:

| Process | `pool_size` | `max_overflow` | Worst case |
|---|---|---|---|
| API (requests and readiness) | 5 | 5 | 10 |
| Worker | 2 | 2 | 4 |

Also set `pool_pre_ping`, a recycle of 30 minutes, a checkout `pool_timeout`
of 10 seconds, and a psycopg `connect_timeout` of 5 seconds. Keep the readiness
probe's `statement_timeout` as `SET LOCAL` inside that transaction. Do not copy
the readiness budget onto the shared pool, and do not set `statement_timeout`
as an engine connect option: a later request would inherit it.

PostgreSQL 16 defaults to `max_connections = 100`, with 3 reserved for
superusers, so about 97 connections are usable. Count every process:

`api_instances * 10 + worker_instances * 4 + short Alembic sessions + admin headroom`

One API and one worker use 14. Stay under about 70 application connections
before raising `max_connections`, so migrations and an operator session still
fit. Each extra API instance adds 10, not 1.

## 2. Environment configuration

Implemented. Startup used to fail when the shell exported `DATABASE_PASSWORD`.

The API reads these database variables, and no others:

- `DATABASE_URL` — API login, used by the shared pool and by readiness
- `MIGRATION_DATABASE_URL` — Alembic
- `WORKER_DATABASE_URL` — worker process pool
- `CC0_MIGRATOR_PASSWORD`, `CC0_API_PASSWORD`, `CC0_WORKER_PASSWORD` — role
  creation in migration 0001

Each URL already carries its password. `DATABASE_PASSWORD` is not one of those
fields. The unknown-name check treats any `DATABASE…` name as application
configuration, so an exported `DATABASE_PASSWORD` was rejected even though
nothing reads it. That exact name is now ignored in `.env` and in the process
environment. A misspelling such as `DATABASE_URLL` is still rejected.
`POSTGRES_PASSWORD` remains the Compose superuser password.

## 3. PostgreSQL permissions

Revision 0002 granted `SELECT, INSERT, UPDATE, DELETE` on future tables to both
`cc0_api` and `cc0_worker` as an M0 stand-in. Revision 0003 revoked those
defaults before creating the catalog and granted the M1 rows of the table
below. At head a new table grants nothing to either role until its own
migration does.

Alembic keeps using `cc0_migrator` for schema changes. `cc0_api` and
`cc0_worker` stay `NOSUPERUSER NOCREATEDB NOCREATEROLE` with schema `USAGE`
only. Neither role gets `CREATE` on a schema. The first migration still
connects as the superuser because `CREATE ROLE` and `CREATE EXTENSION` require
it. Later revisions should run as `cc0_migrator`.

Apply grants in the migration that creates each table, then assert them from
the integration test that already logs in as each role. `cc0_api` and
`cc0_worker` must still fail `CREATE TABLE`.

| Table | `cc0_api` | `cc0_worker` |
|---|---|---|
| `creators`, `assets` | `SELECT` | `SELECT`, `INSERT`, `UPDATE` |
| `asset_embeddings` | `SELECT` | `SELECT`, `INSERT` |
| `ready_assets` (view) | `SELECT` | none |
| `providers`, `source_records`, `rights_evidence`, `jobs` | none | `SELECT`, `INSERT`, `UPDATE` |
| `users` | `SELECT`, `INSERT` | none |
| `boards`, `board_items` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` | none |
| `exports` | `SELECT`, `INSERT`, `UPDATE` | `SELECT`, `UPDATE` on `state` and `object_key` only |

`asset_embeddings` is insert-only so a model rollback is a pointer change.
`asset_relations` is omitted until a later milestone needs stored edges. The
`ready_assets` view is how asset detail reads a landing URL and the latest
evidence row. The view keeps that row when it is verified CC0 and the asset is
READY, without granting `source_records` or `rights_evidence`.

Deletion is narrow. The API deletes boards and board items because those are
user-owned. M1 gave the worker no `DELETE` at all: dead jobs stay as the dead
letter, and an asset row is only inserted once it is `STORED`, so there is no
half-built asset to clean up. Add a worker `DELETE` only when a later kind needs
one, and only on the rows that kind creates. The worker never deletes boards,
users, rights evidence, or source records. Rights
evidence stays append-only. Sequences get `USAGE, SELECT` only for roles that
insert into the table.

`exports` is shared on purpose: the API enqueues a row, and the worker fills
in status and the object key. The worker grant should name those columns when
the table is created, not `UPDATE` on the whole row.
