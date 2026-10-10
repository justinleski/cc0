# cc0

Local-first CC0 visual boards. This checkout is the folder scaffold and the local data services. The API, worker, and frontend are not in this slice.

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

The first Postgres start enables the `vector` extension. Tables and database roles come with Alembic later.

Stop the services without deleting data:

```bash
docker compose stop
```

`docker compose down -v` removes the named volumes.
