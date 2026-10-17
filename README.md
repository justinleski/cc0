# cc0

Local-first CC0 visual boards: discover open images and collect them into visual
boards. M0–M2 and M3 catalog browsing are implemented: Postgres with pgvector,
RustFS, ingestion with CC0 rights evidence, hybrid search, and a frontend with
filters, provenance, related images, and visibility-gated media. The backend now supports opt-in local
authentication and persistent private boards; the frontend board UI remains a
later slice.

## Start locally

Requires Docker Compose, Python 3.12+, and Node 22.

```bash
cp -n .env.example .env
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

For board API setup and token commands, see [private boards](docs/systems/boards.md).

See the [documentation index](docs/README.md), [local development guide](docs/local-development.md),
[test commands](docs/lint-test.md), and [MVP spec](docs/mvp-spec.md).
