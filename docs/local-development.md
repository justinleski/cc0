# Local development

`./scripts/start.sh` builds and starts the local data services, runs migrations
once, then starts the API and frontend. It prints the local app, Swagger docs,
readiness, and RustFS console URLs plus common Compose commands. The host
workflow remains available for hot reload and debugging. See the
[root quick start](../README.md)
for both workflows, [lint and tests](lint-test.md) for `./scripts/check.sh`, and
[ingestion](systems/ingestion.md) for worker commands and `INGEST_*` settings.

## Configuration

Copy `.env.example` to `.env` for local overrides (`cp -n` so an existing file
is left alone). Compose reads it when present and passes it to backend containers;
host backend processes read it directly. In containers, Compose overrides the
three database URLs and `OBJECT_STORE_ENDPOINT` with internal `postgres` and
`rustfs` addresses. Host processes use the URLs and endpoint in `.env`.
The API also requires `CURSOR_SIGNING_KEY`, a private 32-byte random value encoded
as 64 hex digits. Generate it once with `openssl rand -hex 32` and put it in
`.env`; Compose passes it to the API container. The API fails startup if it is
missing or malformed. Migration and worker commands do not need it. Tests
provision isolated random keys, including in CI. Use the same value on every
API replica and keep it separate from database and local-auth credentials.
Rotating it invalidates outstanding ranked pagination cursors; clients can
start a new search. See [search](systems/search.md).

Backend fields are
case insensitive. Unknown keys in `.env` fail startup, except the explicit
Compose allowlist: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`POSTGRES_PORT`, `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY`, `RUSTFS_API_PORT`,
`RUSTFS_CONSOLE_PORT`, `S3_BUCKET`, and `API_PORT`.

The database settings the API reads are `DATABASE_URL`, `MIGRATION_DATABASE_URL`,
`WORKER_DATABASE_URL`, `CC0_MIGRATOR_PASSWORD`, `CC0_API_PASSWORD`, and
`CC0_WORKER_PASSWORD`. Each URL already contains its login password. There is
no `DATABASE_PASSWORD` setting. That name is a common shell export and is
ignored, in `.env` and in the process environment, so it does not configure a
login and does not block startup. `POSTGRES_PASSWORD` is the Compose superuser
password, also not read by the API.

Process environment variables are checked separately, because production is
configured by the environment and `extra="ignore"` would otherwise keep a
localhost default when a name is misspelled. A variable is rejected when its
name, compared case-insensitively, is not a Settings field, not on the Compose
allowlist, and not the ignored `DATABASE_PASSWORD`, and it starts with one of:
`APP_`, `DATABASE`, `MIGRATION_`, `WORKER_`, `CC0_`, `OBJECT_STORE_`,
`READINESS_`, `INGEST_`, `EMBEDDING_`, `MODEL_`, `POSTGRES_`, `RUSTFS_`, `S3_`,
`API_`. `DATABASE` has no trailing
underscore, so `DATABASE_URLL` is rejected and `DATABASE_URL` is kept.
Unrelated names (`PATH`, `HOME`, `GITHUB_*`, npm variables) do not use those
prefixes and stay allowed. Only names that are actually set are considered.

If a host port changes, update the corresponding host-process database URLs or
object store endpoint too. The container services still use internal ports.
Keep `S3_BUCKET` and `OBJECT_STORE_BUCKET` consistent. The shared
RustFS access/secret keys are a local shortcut and must match the backend keys.
`APP_ENV` is compared case-insensitively after stripping whitespace, so
`Local` is local. Any other value rejects the documented dummy passwords and
object-store key/secret by exact equality (a longer secret that merely contains
a dummy token is allowed), a database URL whose password is empty or missing,
and an `OBJECT_STORE_ENDPOINT` whose host is `localhost` or `127.0.0.1`. A real
production endpoint is accepted. CI uses local settings.

Process entry points may load `get_settings()` or `Settings()` once. Handlers and
integrations use `request.app.state.settings` and its lifespan resources; never
call the cached singleton inside a request. Tests pass `create_app(Settings(...))`
and enter `TestClient` as a context manager to run startup/shutdown.

## Database primer

The Postgres container runs one **cluster**: roles (logins) are cluster-wide,
while databases such as `cc0` contain schemas, tables, and extensions (`vector`).
A role is an identity; a grant authorizes an operation on a database, schema,
table, or sequence. Schema `USAGE` alone does not authorize table reads.

The `pgdata` named volume holds the cluster on disk. `docker compose stop` and
`docker compose down` preserve it. `docker compose down -v` deletes local data,
including the RustFS volume, and the next startup initializes a fresh cluster.
Migration tests suffix role names because dropping a shared role would affect
other databases too.

Alembic uses the `cc0` superuser through `MIGRATION_DATABASE_URL` because it
creates roles and extensions. `cc0_migrator` has schema `USAGE, CREATE`;
`cc0_api` and `cc0_worker` have `USAGE` and cannot create tables. Revision 0002
set broad M0 defaults for future tables; revision 0003 revokes them and grants
per table instead. The worker can read and write the six catalog tables but not
delete from them. It can insert embeddings and cannot update them. The API can
read `creators`, `assets`, `asset_embeddings`, and the `ready_assets` view. See
[ADR-0003](decisions/0003-database-roles.md) and [storage](systems/storage.md).

Re-executing 0001 while a role already exists preserves that role's password and
still reapplies its attributes (`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
NOINHERIT`). The password is not updated, so readiness can fail until the
operator aligns it with `.env`. `alembic downgrade base` drops the roles, so a
later upgrade creates them again and sets the password from the environment.
Changing `.env` alone does not rotate an existing cluster. Local rotation is a
new volume (which deletes local data), a manual `ALTER ROLE ... PASSWORD ...`,
or downgrade to base plus upgrade. Production rotation is an operations task,
not a migration.

## Local board API authentication

See [private boards](systems/boards.md) for the opt-in auth mode, token CLI,
owner isolation and board routes. The default is `AUTH_MODE=disabled`.
