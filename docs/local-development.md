# Local development

M0 runs data services in Compose and API/frontend processes on the host. See the
[root quick start](../README.md) for installation and startup commands and
[lint and tests](lint-test.md) for `./scripts/check.sh` and individual checks.

## Configuration

Copy `.env.example` to `.env` for local overrides (`cp -n` so an existing file
is left alone). Both Compose and the backend read this file. Backend fields are
case insensitive. Unknown keys in `.env` fail startup, except the explicit
Compose allowlist: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`POSTGRES_PORT`, `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY`, `RUSTFS_API_PORT`,
`RUSTFS_CONSOLE_PORT`, `S3_BUCKET`, and `API_PORT`.

Process environment variables are checked separately, because production is
configured by the environment and `extra="ignore"` would otherwise keep a
localhost default when a name is misspelled. A variable is rejected when its
name, compared case-insensitively, is not a Settings field and not on that
allowlist, and it starts with one of: `APP_`, `DATABASE`, `MIGRATION_`,
`WORKER_`, `CC0_`, `OBJECT_STORE_`, `READINESS_`, `POSTGRES_`, `RUSTFS_`,
`S3_`, `API_`. `DATABASE` has no trailing underscore, so `DATABASE_URLL` is
rejected and `DATABASE_URL` is kept. Unrelated names (`PATH`, `HOME`,
`GITHUB_*`, npm variables) do not use those prefixes and stay allowed. Only
names that are actually set are considered.

If a host port changes, update the corresponding database URLs or object store
endpoint too. Keep `S3_BUCKET` and `OBJECT_STORE_BUCKET` consistent. The shared
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
sets creator-specific defaults for both the Alembic user and `cc0_migrator`:
future tables grant SELECT/INSERT/UPDATE/DELETE to API and worker, and sequences
grant USAGE/SELECT. That table grant is intentionally broad for M0: both roles
can modify rows in future tables. Existing tables are unaffected. M1 may tighten
these grants. See [ADR-0003](decisions/0003-database-roles.md) and
[storage](systems/storage.md).

Re-executing 0001 while a role already exists preserves that role's password and
still reapplies its attributes (`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
NOINHERIT`). The password is not updated, so readiness can fail until the
operator aligns it with `.env`. `alembic downgrade base` drops the roles, so a
later upgrade creates them again and sets the password from the environment.
Changing `.env` alone does not rotate an existing cluster. Local rotation is a
new volume (which deletes local data), a manual `ALTER ROLE ... PASSWORD ...`,
or downgrade to base plus upgrade. Production rotation is an operations task,
not a migration.
