# cc0

Local-first CC0 visual boards. This checkout has the folder scaffold, local data services, and a FastAPI process with health and readiness checks. Catalog, search, boards, and the frontend are later slices.

## Layout

```text
.
├── docker-compose.yml          # Postgres + pgvector and RustFS
├── docker/postgres/init/       # one-time extension setup on a new volume
├── docs/                       # spec and architecture, next
├── frontend/                   # Next.js client, next
├── backend/
│   ├── app/
│   │   ├── api/                # FastAPI routes
│   │   ├── core/               # config and auth
│   │   ├── db/                 # models and repositories
│   │   ├── domain/             # catalog, rights, boards, search
│   │   ├── integrations/       # source APIs, object storage, embeddings
│   │   └── workers/            # ingestion, thumbnails, exports
│   ├── migrations/             # Alembic, next
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

The first Postgres start enables the `vector` extension. Catalog tables come with Alembic later. Until then, the API connects as a local `cc0_api` login rather than the Postgres superuser. Create that login once on the running database:

```bash
docker compose exec -T postgres psql -U cc0 -d cc0 \
  < backend/scripts/create_local_api_role.sql
```

## API

The process exposes two routes:

- `GET /api/v1/health` returns `200` when the process is up. It does not contact Postgres or RustFS.
- `GET /api/v1/ready` returns `200` only when Postgres accepts `SELECT 1` as `cc0_api` and RustFS accepts a path-style `HeadBucket` for the `cc0` bucket. Either failure returns `503` with `postgres` and `object_store` marked `ok` or `error`. The body does not include credentials or driver error text.

Each response includes `X-Request-ID`. The API logs that id with the method, path, and status. It does not log query strings, tokens, or signed URLs.

From the repository root, with the data services already up:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[dev]"
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In another shell:

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/ready
backend/.venv/bin/pytest backend/tests
```

Stop the services without deleting data:

```bash
docker compose stop
```

`docker compose down -v` removes the named volumes.
