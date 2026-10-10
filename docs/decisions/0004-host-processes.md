# ADR-0004: Application Processes Run on the Host

Status: Accepted

## Context

Docker Compose could run the API, the future worker, and the frontend alongside
PostgreSQL and RustFS. Doing so means a rebuild or a bind-mount dance on every
code change, while the thing that actually needs containers locally is the
stateful dependencies that are tedious to install.

## Decision

Compose runs data services only: `postgres`, `rustfs`, and the one-shot
`rustfs-init`. The FastAPI process, Alembic, pytest, and the Next.js dev server
run on the host against `127.0.0.1` endpoints. The backend is Python for both
the API and the future ingestion worker, so there is one runtime to install.
Containerizing the application is deferred to the AWS milestone.

## Consequences

+ Code changes take effect without rebuilding an image, and a debugger attaches
  to a normal local process.
+ `./scripts/check.sh` runs lint and unit tests before touching Docker, so a
  code failure does not wait on container images.
+ Only two images need to be pulled for a working local stack.
- Local and production runtimes differ, so container-specific problems surface
  late.
- Contributors need Python 3.12 and Node 22 installed, not just Docker. The
  check script fails early with setup instructions when the virtualenv or
  `node_modules` is missing.
- Service URLs differ by context: host processes use `127.0.0.1:9000` while
  `rustfs-init` uses `rustfs:9000` on the Compose network.
