# Catalog frontend (M3 slice 1)

Status: implemented. Related: [search](search.md), [API](api.md),
[storage](storage.md), [frontend commands](../../frontend/README.md).

## Data flow and contracts

The Next.js server calls FastAPI through `frontend/src/api/catalog.ts`, with
`cache: no-store`, a 30-second timeout and redirects rejected. This focused layer
adapts backend cards/details to the existing Work presentation model; object keys
are not used as browser URLs. Search parameters and links live in
`catalog-query.ts`. React components render those models and submit native GET forms. Search/filter submissions request a fresh document;
this avoids a stalled client-router transition observed in the production browser
check. Pagination and asset navigation retain the existing links.

| Interaction | Existing backend contract |
|---|---|
| Browse/search | `GET /api/v1/assets?q=&creator_id=&location=&year_from=&year_to=&limit=&cursor=` |
| Creator choices | `GET /api/v1/creators?query=&limit=50` |
| Detail/provenance | `GET /api/v1/assets/{uuid}` |
| Related images | `GET /api/v1/assets/{uuid}/related?limit=12` |
| Image bytes (new) | `GET /api/v1/assets/{uuid}/image/{thumbnail|original}` |

Cards now include width and height from the existing view, so the masonry layout
can size real assets. This is an additive response change; existing fields,
ranking, cursors, database schema and grants are preserved. Ordering remains
newest first for browse and relevance for search. There is no arbitrary sort
parameter in the existing API. Quotes-only queries use lexical ranking;
unquoted text uses existing lexical/vector fusion. Exact filters and all ranking
remain backend responsibilities. Related images use the existing M2 endpoint,
not board recommendations or M4 work.

## Media and visibility

The browser calls the Next.js same-origin image route. FastAPI validates UUID
and variant; `AssetMediaService` reads detail via `SearchRepository` and
`ready_assets`, obtains the trusted object key, and reads bytes through the
existing S3-compatible ObjectStore. It checks visibility again after object I/O.
Absent, unpublished, suppressed or currently ineligible assets return 404. An
absent object returns 404; storage transport failures return a sanitized 503.
Catalog responses and media use `Cache-Control: no-store`; images also use
`X-Content-Type-Options: nosniff`. The frontend rejects non-image upstream
content and uses unoptimized images to avoid the Next.js optimizer cache.

The bucket remains private. No signed storage URL, arbitrary object-key route,
or frontend copy of the public visibility predicate is introduced. The request
path preserves ADR-0005: router → service → repository → queries. ADR-0001
continues to hold: bytes stay in RustFS/S3. The frontend needs a Next.js server
for these requests, consistent with the spec's static hosting qualification
“where feasible.” Public immutable URLs would conflict with the required
revocation behavior, so this slice uses checked reads instead.

Already delivered/downloaded bytes cannot be recalled, and a check cannot be
atomic with network delivery. The guarantee covers new requests against current
visibility, including a recheck after object retrieval. Media is buffered, bounded
by ingestion's existing image-size limits; large-scale streaming/CDN deployment
is outside this slice.

## UI states and scope

The existing header, guided search results, masonry and tile design remain.
Forms preserve search/filter parameters and drop cursors on changes. Next/first
links use backend cursors without reordering results. Route loading, catalog
errors with retry, empty pages, detail 404, related loading/empty/error and image
failure fallbacks are explicit. Provenance links permit only HTTP(S) URLs without
credentials. Mock fixtures are archived outside `public/`; old numeric mock detail
IDs fail UUID validation. Mock boards display a deferred-feature message, with
no persistent boards, auth, exports or new recommendation algorithm.

## Validation and limits

`npm test` exercises adaptation, filters, pagination, ordering, provenance,
empty/loading/error states and image failures. `test_asset_media.py` verifies
visibility before and after object I/O and sanitized failures. Real database and
RustFS tests verify successful originals/thumbnails, unpublished and revoked
access, and keyset pagination. `npm run test:e2e` exercises the actual frontend
against a running backend; see the frontend README for setup.

The default embedding provider is still deterministic fake. This verifies the
semantic request path, not real visual relevance. OpenCLIP needs a selected
checkpoint, optional dependencies and re-embedding, as documented in
[search](search.md#changing-the-checkpoint). Ranked result caps (100 fused or 50
quotes-only) and creator lookup limits are inherited. The local seed can use the
recorded ingestion fixtures; these become real database/object-store assets,
but use synthetic test images and are not a live museum ingest.

## Acceptance run — 2026-10-16

- `./scripts/check.sh`: backend lint, 215 unit tests, frontend typecheck,
  18 frontend tests, frontend lint, migrations, and 43 backend integration tests
  passed. The backend reports one existing Starlette/AnyIO deprecation warning.
- Final `npm run build`: passed, including lint and type validation.
- `npm run test:e2e`: passed in Chromium against the production Next.js server
  on port 3011 and FastAPI on port 8011, with real local Postgres and RustFS.
  Ten previously stored fixtures were published using the existing fake embedding
  worker. The check navigated newest-first pages, searched `blue coastal` and a
  quoted title, applied an exact location filter to produce an empty page, opened
  Dancer Resting, checked its provenance and original bytes, and followed a related
  work. It also confirmed the missing-asset page. No browser API interception was
  used. The generated screenshot is `frontend/test-results/catalog-provenance.png`.

No blocker remains for this slice's catalog integration. Real semantic relevance
still requires the previously deferred OpenCLIP checkpoint and re-embedding.
The demonstrated catalog uses synthetic ingestion fixtures; live provider ingest
and production storage identities remain separate operational work. No schema,
authentication, persistent boards, board recommendations or exports were added.
