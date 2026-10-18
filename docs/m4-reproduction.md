# Reproducing M4 locally

Covers board recommendations, background exports, and the evaluation run. Each
step names what to expect. Progress and open items are in the
[M4 checklist](m4-checklist.md); the export design is in
[exports](systems/exports.md), the ranking design in
[recommendations](systems/recommendations.md) and [evaluation](evaluation.md).

## 1. Stack and data

```bash
cp -n .env.example .env            # then set CURSOR_SIGNING_KEY, AUTH_MODE=local, AUTH_LOCAL_SIGNING_KEY
python3 -m venv backend/.venv && backend/.venv/bin/pip install -e "backend[dev]"
docker compose up -d --wait postgres rustfs && docker compose run --rm rustfs-init
backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
cd backend && .venv/bin/python -m app.worker ingest      # fixtures, then embeddings
```

Host API and frontend: `cd backend && .venv/bin/uvicorn app.main:app` and
`cd frontend && npm ci && npm run dev` (Node 20.12+). With
`docker compose up --build -d` the API and frontend run in containers; the
worker is always a command you run.

## 2. The one-command journey

```bash
cd backend && .venv/bin/python scripts/m4_e2e.py
```

Creates a throwaway database and bucket, ingests the fixtures, starts an API on a
free port, and walks board → pins → both recommendation modes → pin change →
export → worker → download, checking ownership at each step and the archive
against its manifest. It prints one `ok` line per check, deletes what it created,
and exits non-zero on the first failure. Your development database and bucket
are not touched.

## 3. By hand

1. Mint tokens: `cd backend && .venv/bin/python -m app.auth token alice` (and `bob`).
2. Open http://127.0.0.1:3000, sign in with the token, create a board, and save
   three works from search.
3. On the board page, switch between *More like this board* and *Complement
   (experimental)*. Saving or removing a pin refreshes the suggestions; a saved
   pin never appears among them.
4. Choose **Export board**. The status reads "Preparing your export" until the
   worker runs it:

   ```bash
   cd backend && .venv/bin/python -m app.worker exports
   ```

   The page updates itself; **Download ZIP** fetches a fresh 60-second link.
5. Unzip it: `images/`, `attribution.csv`, `manifest.json`. Each image's SHA-256
   equals its manifest entry.
6. Sign in as Bob and open Alice's board or export URLs: both return 404.
7. Try the failure paths: stop RustFS and run the worker (the export stays
   `QUEUED` and retries; after the retry budget it is `FAILED`, and
   `python -m app.worker redrive --all-dead` requeues it); reject an asset's
   rights and request the export again (it is listed under `excluded`); run
   `python -m app.worker exports-cleanup` after setting `expires_at` in the past.

## 4. Tests

```bash
./scripts/check.sh                              # lint, unit, frontend, migration, integration
backend/.venv/bin/pytest backend/tests/integration/test_exports.py backend/tests/integration/test_m4_e2e.py
```

`test_m4_e2e.py` is skipped on a tree without the recommendation engine, so a
skip there is not a pass. Ranking evaluation commands and snapshot results are in
[evaluation](evaluation.md#board-recommendations).
