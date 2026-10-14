# Lint and tests

`./scripts/check.sh` from the repository root runs everything below in order and
stops at the first failure. Run the individual commands when you want a shorter
loop.

## One-time setup

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e "backend[dev]"
(cd frontend && npm ci)
```

Python 3.12 or newer. Node 22, the version in `frontend/.nvmrc`. CI pins Python
3.12 and Node 22. `npm ci` installs the exact lockfile versions.
CI runs the check script sequentially, then runs `npm run build` in a separate
`Production frontend build` job after the check passes. The build job installs
the locked frontend dependencies with `npm ci`.
New runs cancel superseded runs for the same branch or pull request
(`github.head_ref || github.ref`); a different branch is left running.
setup-python caches pip downloads.

## Checks that need nothing running

```bash
backend/.venv/bin/ruff check backend
backend/.venv/bin/pytest backend/tests/unit
(cd frontend && npm run typecheck)
(cd frontend && npm run lint)
(cd frontend && npm run build)
```

The production build is a separate CI check, so a failed Next.js build has its
own status. Run it locally after `npm ci` when changing the frontend.

Pass the `backend/tests/unit` path, or run bare `pytest` from inside `backend/`,
where `testpaths` in `backend/pyproject.toml` selects the same directory. Either
way the everyday run never touches Docker. Bare `pytest` from the repository
root has no config to read and would collect the integration tests too.

Unit tests cover the health and readiness responses with the dependencies faked,
the pool settings, the role SQL quoting, secret redaction, and the ingestion
pieces that need no database: the rights gate and metadata allowlist
(`test_rights_gate.py`), URL, redirect, size, and status rules of the fetch gate
with a mock transport and a stubbed resolver (`test_fetch_gate.py`), bounded
decode and content keys (`test_images.py`), and backoff and dedupe keys
(`test_job_policy.py`). Embedding and search unit tests
(`test_embeddings.py`, `test_search_text.py`, `test_object_store.py`) use the
deterministic fake provider and assert the vectors it returns, plus quote
parsing, rank fusion, the fake identity, and object-body read failures.
None of them opens a socket, downloads weights, or does a DNS lookup.

`python -m app.embeddings` loads OpenCLIP after `pip install -e "backend[embeddings]"`
and checks that a related image/text pair outranks an unrelated one. It compares
order, not bit-exact floats. The check script does not run it.

The frontend is still the mock browse UI with no API calls, so it has a type
check and a lint but no test suite. Component tests wait until interaction-heavy UI.

## Checks that need the local data services

```bash
docker compose up -d --wait postgres rustfs
docker compose run --rm rustfs-init
backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
backend/.venv/bin/pytest backend/tests/integration
```

These fail rather than skip when Postgres or RustFS is unreachable, so a green
run means the stack really answered. They are the acceptance check through M2:

- `test_migration.py` migrates a throwaway database, then asserts the `vector`
  and `pg_trgm` extensions, the three login roles, that `cc0_api` and
  `cc0_worker` cannot create tables, that `cc0_migrator` can, that the catalog
  and `asset_embeddings` exist and boards do not, that a new table grants
  nothing to API or worker by default, the per-table grants including
  insert-only embeddings and `SELECT` on `ready_assets`, that existing
  passwords survive reapplication, that the downgrade reverses it, and that
  downgrading 0006 deletes a succeeded embed job while an ingest job remains.
- `test_ingestion.py` is the M1 acceptance list against real Postgres and
  RustFS, with the provider replayed from recorded fixtures. Ten CC0 fixtures
  store the same way twice; non-CC0 and unknown fixtures leave no asset, image
  request, or object; a fresh negative cache makes no request; a due recheck
  appends evidence; transient failures retry and then dead-letter; refused
  images are dead on the first attempt; dead jobs survive a repeat ingest and
  redrive; and a crash after the object writes, including a killed worker whose
  lease expires, resumes without a second download. It migrates its own
  database per module and writes to a `cc0-test-*` bucket per test, so the
  operator's catalog and `cc0` bucket are never touched.
- `test_search.py` embeds CC0 fixtures with the fake provider while
  `MODEL_VERSION` is an OpenCLIP checkpoint name, checks that a repeat does not
  write a second vector, that a quoted title is required, that creator, year,
  and location filters hold, that a `STORED` asset is not in search, and that
  `cc0_api` can read `ready_assets` without reading `source_records`. The
  worker cannot `UPDATE` an embedding row. A newer rejection suppresses the
  asset and removes it from search, detail, and related. Fake rows do not
  satisfy an OpenCLIP `enqueue_missing`. An embed worker claims only its
  loaded checkpoint. A ranked cursor keeps its snapshot after the first hit is
  suppressed, and a cursor for another query or model is rejected.
- `test_stack.py` runs a pgvector distance query as `cc0_api`, calls
  `HeadBucket` on the `cc0` bucket, and asserts `GET /api/v1/health` and
  `GET /api/v1/ready` both return `200` against the running stack.

- `test_unavailable.py` points Postgres and the object store at `192.0.2.1`
  (TEST-NET) so the failure waits on the timeout instead of a localhost
  connection refusal. The 503 body reports both checks and contains no secrets
  or URLs. It does not stop Compose.

Unit and integration suites may run in one `pytest` process. Alembic's
`fileConfig` would otherwise disable every logger created before a migration
ran, silencing `caplog` in later tests; `migrations/env.py` passes
`disable_existing_loggers=False` to prevent that.

Stop the services without deleting data:

```bash
docker compose stop
```
