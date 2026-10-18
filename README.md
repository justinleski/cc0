# cc0

Local-first CC0 visual boards: discover verified CC0 images and collect them into visual
boards. M0–M3 are implemented: Postgres with pgvector,
RustFS, ingestion with CC0 rights evidence, hybrid search, and a frontend with
filters, provenance, related images, and visibility-gated media. Opt-in local
authentication protects persistent private boards in the API and frontend.

## Start locally

For the complete container stack, provision a private cursor key once, then start Compose:

```bash
cp -n .env.example .env
grep -q '^CURSOR_SIGNING_KEY=' .env || printf 'CURSOR_SIGNING_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
./scripts/start.sh
```

This builds the API and Next.js images, starts PostgreSQL and RustFS, creates the
local bucket, applies migrations, and starts the API and frontend in that order.
The script prints the app, Swagger API docs, readiness endpoint, RustFS console,
and useful Compose commands after startup. Open http://127.0.0.1:3000; API
readiness is at http://127.0.0.1:8000/api/v1/ready. Copy
`.env.example` to `.env` and edit it to enable local authentication or change
local settings. Keep the generated key private and stable across API restarts.
To run worker or token commands with the same backend image:

```bash
docker compose run --rm --no-deps api python -m app.worker ingest
docker compose run --rm --no-deps api python -m app.worker jobs --state dead
docker compose run --rm --no-deps api python -m app.auth token alice
```

The fixture ingest intentionally exits 1 after storing 10 eligible assets and
dead-lettering 3 refused fixtures. Run `docker compose stop` to preserve data.

### Host processes with hot reload

This workflow requires Docker Compose, Python 3.12+, and Node 22. Stop the
containerized API and frontend before starting host processes on the same ports.

```bash
cp -n .env.example .env
grep -q '^CURSOR_SIGNING_KEY=' .env || printf 'CURSOR_SIGNING_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[dev]"
(cd frontend && npm ci)
docker compose up -d --wait postgres rustfs
docker compose run --rm rustfs-init
backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

`cp -n` leaves an existing `.env` alone.

In another terminal:

```bash
(cd frontend && npm run dev)
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/ready
```

Ingest the recorded Met fixtures (no network access needed):

```bash
(cd backend && .venv/bin/python -m app.worker ingest)
(cd backend && .venv/bin/python -m app.worker jobs --state dead)
# For existing STORED assets without embedding jobs:
(cd backend && .venv/bin/python -m app.worker reembed)
```

The first run stores 10 assets and dead-letters 3 refused fixtures by design, so
it exits 1. See [ingestion](docs/systems/ingestion.md).

Open http://127.0.0.1:3000. Run `./scripts/check.sh` for the full acceptance check.
Use `docker compose stop` to stop services and preserve data.

For local board sign-in and token commands, see [private boards](docs/systems/boards.md)
and the [frontend walkthrough](frontend/README.md).

See the [documentation index](docs/README.md), [local development guide](docs/local-development.md),
[test commands](docs/lint-test.md), and [MVP spec](docs/mvp-spec.md).
