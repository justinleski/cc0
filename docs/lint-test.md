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
CI runs the check script sequentially; there are no parallel CI jobs yet.
New runs cancel superseded runs for the same branch or pull request
(`github.head_ref || github.ref`); a different branch is left running.
setup-python caches pip downloads.

## Checks that need nothing running

```bash
backend/.venv/bin/ruff check backend
backend/.venv/bin/pytest backend/tests/unit
(cd frontend && npm run typecheck)
(cd frontend && npm run lint)
```

Pass the `backend/tests/unit` path, or run bare `pytest` from inside `backend/`,
where `testpaths` in `backend/pyproject.toml` selects the same directory. Either
way the everyday run never touches Docker. Bare `pytest` from the repository
root has no config to read and would collect the integration tests too.

Unit tests cover the health and readiness responses with the dependencies faked,
the role SQL quoting, and secret redaction.

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
run means the stack really answered. They are the M0 acceptance check:

- `test_migration.py` migrates a throwaway database, then asserts the `vector`
  extension, the three login roles, that `cc0_api` and `cc0_worker` cannot
  create tables, that `cc0_migrator` can, that default table/sequence grants work
  for both creators, that existing passwords survive reapplication, and that
  the downgrade reverses it.
- `test_stack.py` runs a pgvector distance query as `cc0_api`, calls
  `HeadBucket` on the `cc0` bucket, and asserts `GET /api/v1/health` and
  `GET /api/v1/ready` both return `200` against the running stack.

- `test_unavailable.py` points Postgres and the object store at `192.0.2.1`
  (TEST-NET) so the failure waits on the timeout instead of a localhost
  connection refusal. The 503 body reports both checks and contains no secrets
  or URLs. It does not stop Compose.

Stop the services without deleting data:

```bash
docker compose stop
```
