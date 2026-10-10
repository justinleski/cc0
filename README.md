# cc0

Local-first CC0 visual boards. This checkout has the folder scaffold, local data services, a FastAPI process with health and readiness checks, and a frontend that browses a mock catalog. Catalog, search, and real boards are later slices.

## Layout

```text
.
├── docker-compose.yml          # Postgres + pgvector and RustFS
├── docker/postgres/init/       # one-time extension setup on a new volume
├── docs/                       # spec, lint and test commands
├── scripts/check.sh            # lint, tests, migration, integration tests
├── frontend/                   # Next.js client, mock catalog until the API serves one
├── backend/
│   ├── app/
│   │   ├── api/                # FastAPI routes
│   │   ├── core/               # config and auth
│   │   ├── db/                 # models and repositories
│   │   ├── domain/             # catalog, rights, boards, search
│   │   ├── integrations/       # source APIs, object storage, embeddings
│   │   └── workers/            # ingestion, thumbnails, exports
│   ├── migrations/             # Alembic revisions
│   ├── scripts/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── fixtures/
│       └── e2e/
└── infra/aws/                  # not used until a later milestone
```

## Local data services

```bash
docker compose up -d postgres rustfs rustfs-init
docker compose ps
```

Compose uses the defaults in `.env.example`. Copy that file to `.env` only if you need different ports or passwords. If something else is already bound to host port 5432, set `POSTGRES_PORT` in `.env`.

Postgres is on `127.0.0.1:5432` unless `POSTGRES_PORT` overrides it. RustFS's S3 API is on `127.0.0.1:9000` and its console is on `127.0.0.1:9001`. `rustfs-init` creates the `cc0` bucket and exits. Running it again does not create a second bucket.

Local object storage is RustFS (`rustfs/rustfs:1.0.1`), an S3-compatible server aimed at development. AWS S3 is the later production store. The application should use path-style S3 requests against RustFS. MinIO is not part of this stack; its community edition was discontinued.

The first Postgres start enables the `vector` extension. The Alembic migration enables it again if that step was skipped, and creates three logins: `cc0_migrator`, `cc0_api`, and `cc0_worker`. There are no catalog tables yet. The migration runs as the database superuser, because creating roles and extensions requires it. `cc0_migrator` can create objects in `public`. `cc0_api` and `cc0_worker` can connect and use the schema, and cannot create tables. The API readiness check uses `cc0_api`.

## API

The process exposes two routes:

- `GET /api/v1/health` returns `200` when the process is up. It does not contact Postgres or RustFS.
- `GET /api/v1/ready` returns `200` only when Postgres accepts `SELECT 1` as `cc0_api` and RustFS accepts a path-style `HeadBucket` for the `cc0` bucket. Either failure returns `503` with `postgres` and `object_store` marked `ok` or `error`. The body does not include credentials or driver error text.

Each response includes `X-Request-ID`. The API logs that id with the method, path, and status. It does not log query strings, tokens, or signed URLs.

From the repository root, with the data services already up:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[dev]"
backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In another shell:

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/ready
```

## Checks

```bash
./scripts/check.sh
```

That runs backend lint, unit tests, the frontend type check and lint, then starts the data services, migrates, and runs the integration tests. The integration tests fail rather than skip when Postgres or RustFS is down, so a green run means the stack answered. `docs/lint-test.md` lists the same commands individually. `backend/.venv/bin/pytest backend/tests/unit` is the fast loop that needs nothing running.

Stop the services without deleting data:

```bash
docker compose stop
```

`docker compose down -v` removes the named volumes.
