# cc0

Local-first CC0 visual boards: discover open images and collect them into visual
boards. M0 is implemented: Postgres with pgvector, RustFS, migrations, and a
FastAPI health/readiness API. The frontend browses a mock catalog; ingestion,
search, and persistent boards are future milestones.

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

Open http://127.0.0.1:3000. Run `./scripts/check.sh` for the full acceptance check.
Use `docker compose stop` to stop services and preserve data.

See the [documentation index](docs/README.md), [local development guide](docs/local-development.md),
[test commands](docs/lint-test.md), and [MVP spec](docs/mvp-spec.md).
