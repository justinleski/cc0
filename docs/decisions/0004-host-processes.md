# ADR-0004: Local Host and Container Workflows

Status: Accepted

## Context

The initial local setup ran only PostgreSQL and RustFS in Compose. The API,
worker, and frontend ran on the host to keep code changes fast. The planned AWS
pilot runs application containers, so a local container path is useful before
deployment.

## Decision

Compose supports both workflows. `docker compose up --build -d` builds the
backend and frontend, starts `postgres` and `rustfs`, runs the one-shot
`rustfs-init` and `migrate` services, then starts `api` and `frontend`. The same
backend image runs worker and token CLI commands on demand. The host workflow
starts only data services in Compose and runs Alembic, FastAPI, the worker, and
Next.js on the host. Compose uses service DNS names inside containers and binds
published local ports to `127.0.0.1`.

## Consequences

+ The host path keeps hot reload and normal-process debugging; the container
  path checks application image startup before AWS deployment.
+ `./scripts/check.sh` runs lint and unit tests before touching Docker, so a
  code failure does not wait on container images.
+ Container startup applies migrations before the API and waits for API readiness
  before starting the frontend.
- Image rebuilds take time after code changes. The host workflow still needs
  Python 3.12 and Node 22 and remains the path for `./scripts/check.sh`.
- Service URLs differ by context: host processes use forwarded `127.0.0.1`
  ports, while containers use `postgres`, `rustfs`, and `api` DNS names.
- The local images do not supply AWS credentials, HTTPS, backups, or production
  authentication; those remain part of the AWS milestone.
