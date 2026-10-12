#!/usr/bin/env bash
# M0 and M1 acceptance check. Run from the repository root: ./scripts/check.sh
#
# Lint and unit tests run first so a code failure does not wait on container
# images. Everything after that needs the local data services and fails when
# they are down.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV=backend/.venv

step() {
    printf '\n== %s\n' "$1"
}

if [ ! -x "$VENV/bin/python" ]; then
    echo "Missing $VENV. Create it first:" >&2
    echo "  python3 -m venv $VENV && $VENV/bin/pip install -e \"backend[dev]\"" >&2
    exit 1
fi

if [ ! -d frontend/node_modules ]; then
    echo "Missing frontend/node_modules. Install first:" >&2
    echo "  (cd frontend && npm ci)" >&2
    exit 1
fi

step "backend lint"
"$VENV/bin/ruff" check backend

step "backend unit tests"
"$VENV/bin/pytest" backend/tests/unit

step "frontend type check"
(cd frontend && npm run --silent typecheck)

step "frontend lint"
(cd frontend && npm run --silent lint)

if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. The integration tests need it:" >&2
    echo "  docker compose up -d postgres rustfs rustfs-init" >&2
    exit 1
fi

step "local data services"
docker compose up -d --wait postgres rustfs
# Separate run so a bucket-creation failure is this script's exit code.
docker compose run --rm rustfs-init

step "migration"
"$VENV/bin/alembic" -c backend/alembic.ini upgrade head

step "backend integration tests"
"$VENV/bin/pytest" backend/tests/integration

printf '\nAll checks passed.\n'
