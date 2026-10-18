# Search and embeddings

Status: Implemented (M2: default fake provider, OpenCLIP optional and unpinned)
Owner: [backend/app/embeddings/](../../backend/app/embeddings/service.py), [backend/app/search/](../../backend/app/search/service.py), [migration 0006](../../backend/migrations/versions/0006_embeddings.py)
Depends on: PostgreSQL pgvector, RustFS, the M1 catalog
Related: [ingestion.md](ingestion.md), [storage.md](storage.md), [api.md](api.md), [ADR-0002](../decisions/0002-postgres-pgvector.md), [MVP spec M2](../mvp-spec.md#milestone-m2--image-embeddings-and-hybrid-search)

## Summary

Ingest stops at `STORED`. A separate `embed` job reads the original from
RustFS, inserts one vector for the loaded provider's identity, and sets the
asset `READY`. Public search, detail, and related read `ready_assets`: status
`READY` and the latest rights evidence row is verified CC0. Non-CC0 and unknown
records still get no asset, no object, and no vector.

The column is `vector(512)`, the width of the ViT-B family. OpenCLIP's identity
is `MODEL_KEY` and `MODEL_VERSION`. `MODEL_VERSION=unpinned` does not download
weights. The fake provider's identity is `fake` / `deterministic-v1`,
independent of those two settings. CI and the default local provider are that
fake. Real OpenCLIP is an optional extra and a manual command.

Query text is encoded for that request and dropped. It is not a row in
`asset_embeddings`.

## Architecture

```mermaid
flowchart TD
  ingest["ingest job"] --> stored["asset STORED"]
  stored --> embedJob["embed job"]
  embedJob --> read["GET original from RustFS"]
  read --> vec["INSERT asset_embeddings"]
  vec --> ready["asset READY"]
  ready --> api["GET /api/v1/assets"]
  api --> phrase{"balanced quotes?"}
  phrase -->|"phrase required"| lex["unstemmed lexeme filter"]
  phrase -->|"unquoted text"| textEnc["one text embedding, then SQL"]
  textEnc --> cosine["exact cosine on the active model"]
  lex --> fuse["RRF k=60"]
  cosine --> fuse
```

### Feature-based layered modular monolith

The backend convention and incremental adoption policy are recorded in
[ADR-0005](../decisions/0005-feature-based-layered-architecture.md).

Search is packaged by feature, with HTTP, orchestration, persistence, and SQL
construction separated inside that feature. These are in-process module
boundaries, not independently deployed services. The arrangement supports
DDD-style boundaries but does not by itself implement DDD. PostgreSQL remains
the intentional search backend; there is no generic repository framework,
abstract repository interface, Unit of Work, or new dependency.

```text
app/search/
├── router.py
├── service.py
├── repository.py
├── queries.py
├── models.py
├── cursor.py
├── parse.py
└── rank.py
```

Read the modules in the following order to follow a request. The package also
contains its Python `__init__.py` marker.

| Module | Responsibility and functions to inspect |
|---|---|
| [router.py](../../backend/app/search/router.py) | HTTP endpoints, Pydantic response models, error translation, and `_service()` wiring |
| [service.py](../../backend/app/search/service.py) | `search()`, `_ranked()`, `_browse()`, `_page_snapshot()`, `_suggest()`: validation, embedding generation, RRF coordination, pagination decisions, and suggestions |
| [repository.py](../../backend/app/search/repository.py) | Concrete `SearchRepository`: connections, query execution, `_candidate_ids()` and `_card()` row mapping; `asset()`, `related()`, `creators()`, `browse()`, `candidates()`, and `cards()` database operations |
| [queries.py](../../backend/app/search/queries.py) | SQL constants, `_filters()`, `_vector_literal()`, `browse_query()`, `lexical_query()`, and `semantic_query()`; builders return SQL and bound parameters without I/O |
| [models.py](../../backend/app/search/models.py) | Result dataclasses and typed filters, browse positions, and candidate lists; no HTTP or database driver types |
| [cursor.py](../../backend/app/search/cursor.py) | Encoding, decoding, browse-position parsing, ranked snapshots, cursor-kind checks, and scope validation |
| [parse.py](../../backend/app/search/parse.py) | Balanced required phrases and stripped stray quotes |
| [rank.py](../../backend/app/search/rank.py) | Candidate limits, snapshot limit, and deterministic RRF fusion |

### How to observe the patterns and invariants

| Pattern or invariant | Where to follow it in the code |
|---|---|
| Composition and dependency injection | `main.create_app()` registers the feature router. `router._service()` supplies the concrete repository and existing embedding provider to the service, using the existing application engine. Service tests supply an in-memory repository through the same constructor. |
| Connection ownership | Repository construction stores the engine without checkout. `service._ranked()` finishes `embed_text()` before `repository.candidates()` opens one connection for both lexical and semantic reads. Context managers release it on success or failure. |
| Public CC0 visibility | All asset SQL in `queries.py` uses `catalog.READY_ASSETS_VIEW`, including the related seed and candidates. Follow migration 0006 for the latest-evidence predicate. Creator lookup uses `creators`. |
| Parameterized SQL | Query builders bind user values separately. Dynamic SQL contains only trusted catalog identifiers, fixed clauses, and generated phrase placeholders. `_filters()` makes quoted phrases mandatory `simple` dictionary predicates. |
| Ranking isolation | `queries.lexical_query()` and `semantic_query()` preserve candidate limits and ID tie-breakers. Semantic and related queries bind both model identity fields. `service._ranked()` calls `rank.fuse()` and applies the snapshot cap. |
| Snapshot pagination | The service constructs scope with `_rank_scope()`; `cursor.require_same_scope()` validates it. Later pages use stored IDs without repeating inference or candidate retrieval. `repository.cards()` rechecks visibility, omits hidden IDs, and restores the stored order. |
| Keyset pagination | `_browse()` requests `limit + 1`; `queries.browse_query()` uses descending `(ingested_at, id)` order and the matching tuple comparison. Cursor serialization stays in `cursor.py`. |
| Thin HTTP boundary | `router.py` translates existing typed results and errors to unchanged response models, status codes, and `/api/v1` routes. The service has no FastAPI imports or SQL statements. |

The changes are structural. HTTP interfaces, cursor formats, ranking semantics,
error behavior, views, and database grants are preserved. The internal service
constructor accepts a concrete repository instead of an engine. SQL is kept in
Python for review alongside its parameter builders; external SQL files and
stored functions are unnecessary here. Ingestion and embedding jobs now use
feature-local layers too; shared worker job and lease machinery stays in the
worker package.

Embedding jobs use the same feature-local layering:
[worker/embed.py](../../backend/app/worker/embed.py) owns draining, model-filtered
claiming, lease lifecycle, retries and failure settlement.
[service.py](../../backend/app/embeddings/service.py) retains payload/model and
asset validation, object reads, inference, and publication sequencing; [models.py](../../backend/app/embeddings/models.py)
holds the result and asset dataclasses; [repository.py](../../backend/app/embeddings/repository.py)
owns connection scopes and claim-fenced writes; and
[queries.py](../../backend/app/embeddings/queries.py) holds SQL and bound-parameter
construction. [provider.py](../../backend/app/embeddings/provider.py) defines
the provider and fake, while [openclip.py](../../backend/app/embeddings/openclip.py)
loads the optional model. The worker runtime supplies the concrete repository,
shared job store and object store, and injects the service into `EmbedWorker`.
The adapter calls `prepare()` before claiming and `execute(job)` inside the lease.
The service exposes its model identity for the claim filter without owning claims.
Both the new-vector and existing-vector
paths lock the claim, publish the asset, and settle the job in one transaction.
Object reads and inference happen after read connections close and before the
write transaction starts.

### Verification

`test_search_service.py` checks orchestration with an in-memory repository.
`test_search_queries.py` checks parameter binding, required phrases, year and
keyset predicates, public-view reads, model filtering, and deterministic order.
`test_search_repository.py` observes actual service/repository call ordering and
connection release when a candidate query fails. Existing PostgreSQL integration
tests remain the authority for executed SQL, rights revocation, and related
visibility. Commands and infrastructure requirements are in
[Lint and tests](../lint-test.md).
`test_embedding_service.py` checks object and inference ordering, while
`test_embedding_repository.py` checks bound parameters and transaction cleanup.
The PostgreSQL search tests exercise rollback and claim fencing for both
publication paths.

## What a row means

- `asset_embeddings` is one vector per `(asset_id, model_key, model_version)`.
  The worker may insert. It may not update or delete. A new checkpoint is a new
  row. Search reads the identity of the provider the API process loaded.
  OpenCLIP uses `MODEL_KEY` and `MODEL_VERSION`. The fake provider stores
  `fake` / `deterministic-v1`, so a fake row is a different index from an
  OpenCLIP checkpoint of the same environment names. Pointing the API at an
  older identity restores that ranking. Rows for the previous identity stay.
- `assets.lexeme_simple` is the `simple` dictionary: creator name and aliases
  at weight A, title and tags at B, description at C. A name like Running stays
  `running`. Quoted phrases match this vector only, so a quote does not stem.
- `assets.lexeme_english` is the `english` dictionary on title, tags, and
  description. Unquoted lexical rank is `ts_rank_cd` on both vectors.
- `ready_assets` picks the latest `rights_evidence` row for the source record
  (`checked_at DESC, id DESC`) and keeps the asset when that row is
  `rights_type` CC0, `verification_status` VERIFIED, and `assets.status` is
  READY. Since revision 0009 it also requires that no other evidence row at the
  same `checked_at` is anything but verified CC0. The `id DESC` tie-break is a
  random UUID, so a tied rejection must not depend on which row sorts first.
  Browse, ranked search, related candidates, asset detail, and
  [board recommendations](recommendations.md) all read the view. `cc0_api` selects the view and has no privilege on `source_records`
  or `rights_evidence`. The view owner is the migration superuser
  (`security_invoker = false`).
- A newer evidence row that is not verified CC0 is written by
  `IngestionRepository.record_superseding_rights`, which sets the asset `SUPPRESSED`
  in that same transaction. Ingest treats an asset that already has bytes as
  finished and does not re-run the rights gate, so this function is how a
  published asset is unpublished. Public queries require both gates. A
  suppression or a latest decision that is not verified CC0 removes the asset
  from search, detail, and related. Detail is 404.
- `pg_trgm` suggests a creator name. It fixes a typo such as `Vincent van Goh`
  toward `Vincent van Gogh`. It does not expand `DaVinci` into `Leonardo da
  Vinci`, and it does not rerank assets.

## Search

`GET /api/v1/assets` takes `q`, `creator_id`, `location`, `year_from`,
`year_to`, `limit` (1–50, default 20), and `cursor`.

- No `q`: newest public assets. No embedding call. Cursor is a keyset on
  `(ingested_at, id)`.
- Balanced `"..." ` spans are required phrases. A query that is only quotes
  ranks by the lexical score and does not encode text.
- Mixed text (`"da vinci" coastal`) keeps the phrase as a filter and fuses the
  unquoted remainder.
- An unpaired quote is removed. `applied_query` is what was searched, so a
  client can show “Searching for …”.
- Unquoted text embeds once, off the event loop (the route is synchronous),
  and before a catalog connection is checked out. Each provider runs one encode
  at a time. Lexical and cosine lists are capped at 50 and fused with `k = 60`.
  The fused order is at most 100 ids, and a quotes-only query stores at most
  50. That list is the whole ranked result for the request.
- Creator, location, and year are SQL predicates. Location is exact. A year
  filter excludes assets with no year.
- An empty quoted phrase returns no assets. An unquoted string that matches no
  words still has nearest neighbors.
- When the first page is empty, `suggestion` may be a similar creator name.

`GET /api/v1/assets/{id}` reads `ready_assets`. A `STORED` asset, a
`SUPPRESSED` asset, and a `READY` asset whose latest evidence is not verified
CC0 are 404.
`GET /api/v1/assets/{id}/related` is exact cosine order of the active model
among other rows of that same view, excluding the seed. `reasons` may include
`same_creator`, `same_location`, and `same_style_tag`. There is no HNSW index
and no `asset_relations` table.

A ranked cursor is bound to the applied query, its phrases and unquoted text,
`creator_id`, `location`, `year_from`, `year_to`, and the active embedding
identity (`model_key`, `model_version`). A cursor for a different query, filter
set, or model is HTTP 400. The first page stores the fused id order. Later
pages return the next slice of that snapshot. Loading a card reads
`ready_assets` again, so a suppressed id is left out and the ids after it keep
the positions from the first page. An asset that becomes public after the first
page appears on a new search. The snapshot cap above is the product limit for
one ranked query.
Ranked cursors are HMAC signed with a dedicated `CURSOR_SIGNING_KEY` containing
32 random bytes encoded as 64 hex digits. It is required for API startup and
must be identical across API replicas; it is not a database credential or a
published default. Generate it with `openssl rand -hex 32` and provision it
privately through `.env` locally or environment secret injection in production.
Modified payloads return HTTP 400 before card reads. Rotating the key
invalidates outstanding ranked cursors, so clients must restart pagination.
Encoded cursors are limited to 22,000 characters and decoded payloads to 16 KiB;
search text is limited to 2,048 UTF-8 bytes and location to 512 UTF-8 bytes. The existing
page limit is 1–50. Browse keysets remain unsigned because SQL reapplies their
filters on every page.
`GET /api/v1/creators?query=` lists creators and sets `did_you_mean` when the
closest name is not the typed string.

## Embed job

`dedupe_key = embed:{model_key}:{model_version}:{asset_id}` uses the provider
identity. Ingest inserts that row in the same transaction as the asset. The
worker reads `original_key` from object storage. A timeout or client error
while reading the body is `ObjectStoreUnavailable`, the body is closed, and
the embed job retries. If the vector row already exists, inference is skipped
and a `STORED` asset still moves to `READY`. A dead embed job leaves the asset
`STORED`.

An embed worker claims jobs whose payload `model_key` and `model_version`
match the loaded identity. That filter is inside the `FOR UPDATE SKIP LOCKED`
candidate query. A job for another checkpoint stays pending, `retry_wait`, or
leased by its owner, and this worker does not spend its attempts. Ingest
claims are not filtered by model.

`python -m app.worker reembed` finds `STORED` or `READY` assets missing the
loaded identity, queues them, redrives dead embed jobs for that identity, and
drains. A second run does not insert a second vector. Fake rows stay when you
later load OpenCLIP: `enqueue_missing` for the OpenCLIP identity still queues
those assets, and nothing deletes or updates the fake rows.

## Changing the checkpoint

Rebuild with the worker on the new checkpoint while `MODEL_KEY` and
`MODEL_VERSION` match that provider identity:

```bash
.venv/bin/python -m app.worker reembed
```

Confirm coverage: every asset that passes the `ready_assets` predicate has a
row in `asset_embeddings` for that identity. Then start the API so its process
loads the same identity. Environment config alone leaves a running API on the
identity it already loaded. Old rows stay, so starting the API on the previous
identity is the rollback.

## Operator commands

```bash
.venv/bin/python -m app.worker ingest     # ingest, then embed
.venv/bin/python -m app.worker drain      # queued ingest jobs, then embed jobs
.venv/bin/python -m app.worker reembed    # assets missing the active checkpoint
.venv/bin/python -m app.embeddings        # manual OpenCLIP order check
```

The smoke command requires `EMBEDDING_PROVIDER=openclip`, a real
`MODEL_VERSION`, and `pip install -e "backend[embeddings]"`. It prints two
cosines and fails when the related pair does not rank ahead. CI does not run it.

## Later

A process-local LRU for query vectors, keyed by model version plus the
normalized string, is the first cache if repeated queries show text encode in
search latency. Redis stays the later step for more than one API process.
A ranked page is the snapshot stored in its cursor. Search quality benchmarks
stay in milestone M4.

## M3 catalog media

The real [catalog frontend](catalog-frontend.md) consumes these contracts. Cards
include image dimensions. `GET /api/v1/assets/{id}/image/{thumbnail|original}`
reads private object bytes only after a public-view check, rechecks visibility
after retrieval, returns 404 for hidden/missing images and 503 for storage failures.
All catalog responses disable caching. Schema and grants are unchanged.
