# Ingestion

Status: Implemented (M1: one provider, fixture-replayed by default)
Owner: [backend/app/ingestion/](../../backend/app/ingestion/rights.py), [backend/app/worker/](../../backend/app/worker/ingest.py), [migration 0003](../../backend/migrations/versions/0003_catalog.py)
Depends on: PostgreSQL, RustFS
Related: [storage.md](storage.md), [api.md](api.md), [ADR-0003](../decisions/0003-database-roles.md), [ADR-0004](../decisions/0004-host-processes.md), [MVP spec M1](../mvp-spec.md#milestone-m1--ingestion-and-provenance-vertical-slice)

## Summary

A single worker process turns The Met Open Access records into stored CC0
assets, with an append-only record of why each one was allowed. Two gates run
in order. The rights gate reads item metadata and decides whether the item is
explicitly CC0; anything else becomes a metadata-only negative cache row and
nothing else happens. Only after CC0 evidence is written does the fetch gate
request image bytes, under a host allowlist and hard limits. A stored asset
stops at `STORED` (original, thumbnail, SHA-256, evidence); `READY` waits for
embeddings in M2. Nothing in M1 is visible to users.

Tests and the default local run replay recorded fixtures from
[backend/fixtures/met](../../backend/fixtures/met) and open no socket.

## Architecture

```mermaid
flowchart TD
  CLI["python -m app.worker ingest"] --> ENQ["enqueue: upsert source_records"]
  ENQ -->|"negative cache fresh"| SKIP["skip, no request"]
  ENQ -->|"asset STORED"| SKIP
  ENQ --> JOB[("jobs row, dedupe_key ingest:met:id")]
  JOB --> CLAIM["claim: FOR UPDATE SKIP LOCKED + lease"]
  CLAIM --> EVID{"current CC0 evidence?"}
  EVID -->|"no"| META["fetch metadata (allowlisted host)"]
  META --> GATE{"rights gate"}
  GATE -->|"non-CC0 / unknown"| NEG["append evidence, NON_CC0_CACHED or UNKNOWN_CACHED"]
  GATE -->|"Met policy + isPublicDomain true"| CC0["append evidence, ELIGIBLE_CC0"]
  EVID -->|"yes"| FETCH
  CC0 --> FETCH["fetch gate: image GET with caps"]
  FETCH --> PUT["PUT original + thumbnail to RustFS"]
  PUT --> STORE["insert asset STORED, job succeeded"]
  FETCH -->|"transient"| RETRY["retry_wait with backoff"]
  FETCH -->|"permanent or exhausted"| DEAD["jobs.state = dead"]
  RETRY --> CLAIM
```

| Module | Responsibility |
|---|---|
| [met.py](../../backend/app/ingestion/met.py) | Catalog discovery, metadata allowlist, normalization and validation |
| [rights.py](../../backend/app/ingestion/rights.py) | The rights gate. Pure, no I/O |
| [fetch.py](../../backend/app/ingestion/fetch.py) | URL validation, public-address check, image and metadata HTTP clients, pacing |
| [images.py](../../backend/app/ingestion/images.py) | Bounded decode, SHA-256, WebP thumbnail |
| [fixtures.py](../../backend/app/ingestion/fixtures.py) | Recorded responses with the same allowlist rules, plus a request log |
| [worker/ingest.py](../../backend/app/worker/ingest.py) | The pipeline and its skip rules |
| [worker/jobs.py](../../backend/app/worker/jobs.py) | Claim, lease, retry, dead letter, redrive |
| [worker/store.py](../../backend/app/worker/store.py) | Catalog reads and writes |

## What a row means

- `source_records` is the provider's item, unique on `(provider_id, external_id)`.
  It is also the negative cache: classification, eligibility, `next_recheck_at`,
  and allowlisted `raw_metadata` (object id, title, artist, dates, landing URL,
  image URL string, rights fields). Data URIs and the rest of the payload are
  dropped.
- `rights_evidence` is an append-only observation. A recheck inserts a row; no
  code path updates or deletes one. `raw_statement` keeps the provider's own
  text, and `decision_reason` stores the stable token for why the gate decided
  what it did. Evidence attaches to the source record even when no asset ever
  exists.
- `assets` is bytes we actually stored, unique on `source_record_id`. `sha256` is
  indexed but deliberately not unique.
- `jobs` is the lock and the retry ledger, not the audit log. `claim_token` is
  issued on each claim and required on every later write.
- `creators` are provider-scoped: the Met artist authority URL when present,
  otherwise a name key. No cross-provider person merge.

## Important invariants

- No image request happens before CC0 evidence is committed for that record.
  Non-CC0 and unknown records produce no asset row, no image GET, no object
  write, and (structurally, since the table does not exist) no embedding write.
- The only passing claim for the Met is its published Open Access policy plus
  `isPublicDomain: true` (a JSON boolean; a non-boolean is rejected metadata).
  That flag is trusted only because the policy says the hosted image is CC0.
  `decide()` defaults to fail closed, so a later source cannot inherit the
  exception. Without the trusted flag, only an explicit CC0 dedication in the
  item's own statement admits a record.
- Public domain is not CC0. A public-domain mark is `PUBLIC_DOMAIN_MARK` and
  `NON_CC0_CACHED`; those bytes are never fetched. An expired copyright is a
  country-specific conclusion, and a photograph of the work can still be
  copyrighted. CC0 is a waiver from a rights holder.
- A restrictive statement beats the flag: other Creative Commons licenses and
  their URLs, a public-domain mark, copyright, permission, and personal-use
  language. That is `NON_CC0_CACHED`. A statement that both dedicates the work
  to CC0 and restricts it is `UNKNOWN_CACHED`. Empty or unrecognized text may
  pass only together with the trusted flag. The original statement is kept.
- Invalid or missing metadata is `UNKNOWN_CACHED`. Unknown rechecks after
  `INGEST_RECHECK_UNKNOWN_DAYS` (7), firm non-CC0 after
  `INGEST_RECHECK_NON_CC0_DAYS` (30). The deadline uses the database clock. Before
  it passes, enqueue returns without any HTTP call, metadata included.
- A fetch failure never rewrites eligibility. An `ELIGIBLE_CC0` record whose
  image is refused or unreachable keeps its evidence and has no asset; only the
  job dies.
- The fetch gate allows `https` only, no URL credentials, hosts in
  `INGEST_IMAGE_HOSTS` only (metadata uses the separate `INGEST_METADATA_HOSTS`),
  and refuses loopback, private, link-local, reserved, multicast, and
  unspecified addresses. Redirects are followed manually, at most
  `INGEST_MAX_REDIRECTS`, and every hop is re-validated and re-resolved.
- Bodies are streamed and counted against `INGEST_MAX_IMAGE_BYTES` (10 MB)
  whatever `Content-Length` says. Only JPEG, PNG, and WebP are decoded, and the
  pixel count is checked from the header before pixels load.
- Object keys are content-addressed:
  `originals/ab/cd/<sha256>.<ext>` and `thumbnails/ab/cd/<sha256>.webp`. The
  digest is also written as object metadata. The worker never deletes an object.
- The asset row is inserted only after both object PUTs succeed, in the same
  transaction that marks it `STORED` and the job `succeeded`.
- One claimed job at a time per worker, so one in-flight source request. In
  network mode a shared `Pacer` spaces metadata and image requests at
  `INGEST_RATE_LIMIT_RPS`.

## Jobs, retries, and the dead letter

PostgreSQL is the only job store. "Dead letter" is `jobs.state = dead` on the
same row, with `last_error` and `dead_at`, so the failure stays joined to its
source record and evidence. A later SQS or ElasticMQ queue would only wake the
worker; it would not hold a second copy of job state. See the spec's
[architecture notes](../mvp-spec.md) on not running two authoritative job
stores.

States: `pending`, `leased`, `retry_wait`, `succeeded`, `dead`.

- `dedupe_key = ingest:{provider}:{external_id}`, unique. A repeat ingest finds
  the existing row instead of queueing a second one.
- Claim is one `UPDATE` over a `FOR UPDATE SKIP LOCKED` candidate. It sets
  `leased_until`, issues a new `claim_token`, and increments `attempt_count` at
  claim time, so a worker killed mid-job still spends an attempt when its lease
  expires. A reclaim issues a different token. Every later write
  (`succeed`, `retry_later`, `mark_dead`, payload hints, lease renewal, and the
  catalog writes for a rights decision or an asset) matches that token, the
  leased state, and an unexpired lease. Zero rows means the claim was lost, and
  the stale worker does not record a terminal state. A background renewal
  extends the lease while the token is still held, and stops at
  `INGEST_JOB_DEADLINE_SECONDS` (900) so a stalled transfer cannot hold the job
  forever.
- `INGEST_MAX_ATTEMPTS = 4`: one attempt plus three retries.
- Transient (timeout, connection error, DNS failure, 429, 5xx, object store
  unavailable) goes to `retry_wait` with exponential backoff and jitter,
  honoring a numeric `Retry-After`.
- Permanent (other 4xx, allowlist or private-address refusal, non-image body,
  oversize, empty body, missing image URL) goes `dead` immediately without
  spending the transient budget.
- A lease that expires on the last attempt is settled by `reap_exhausted` at
  the start of each drain, because the claim query can no longer see it. If the
  asset is already `STORED`, the job is reconciled as `succeeded`. Otherwise it
  is moved to `dead` with `attempts_exhausted`.
- A repeat ingest never clears `dead`, never steals a live lease, and never
  resets `pending` or `retry_wait`. Those rows keep the attempts and the
  `next_run_at` they already have. A due negative-cache recheck resets attempts
  on a `succeeded` row only, and that condition is checked in the same
  statement as the reset.
- `redrive` is the operator action: `dead` back to `pending` with attempts reset.
  `last_error` is kept.

`jobs.kind` is the unit of independent retry. M1 has only `ingest`. Embeddings
will be a separate `embed` kind with its own key and budget, so a model failure
cannot re-lease the image download.

## Resume without a second download

There is no stage column. Progress is read from durable state on every attempt:

- Fresh negative cache: return, no request.
- Asset already `STORED`: mark the job succeeded.
- `ELIGIBLE_CC0` with verified CC0 evidence and no recheck due: skip the
  metadata request and rebuild asset fields from stored `raw_metadata`.
- After both PUTs the job payload records `stored` (digest, keys, type,
  dimensions). A later attempt heads both keys and compares the stored digest;
  a match skips the image GET and commits the asset (`stored_from_existing_objects`).
  A missing or mismatched object downloads again.

`payload.last_step` (`rights_check`, `download`, `store`) is an operator hint
only; skip logic never trusts it.

## Operator commands

Run from `backend/`, with the stack up and migrations applied:

```bash
.venv/bin/python -m app.worker ingest                 # discover, queue, run
.venv/bin/python -m app.worker ingest --object-id 100001
.venv/bin/python -m app.worker drain                  # run queued jobs only
.venv/bin/python -m app.worker jobs --state dead      # inspect dead letters
.venv/bin/python -m app.worker redrive --dedupe-key ingest:met:400001
.venv/bin/python -m app.worker redrive --all-dead
```

`ingest` and `drain` exit 1 when any job in the batch went `dead`. Against the
recorded catalog a first run stores 10 assets, negative-caches 6 records, and
dead-letters 3 eligible records whose images are refused by design (missing
image URL, off-allowlist host, HTML body). A second run makes no requests.

`INGEST_SOURCE_MODE=network` switches to the live Met API. It is opt-in and
never used by tests. Regenerate fixtures with
`backend/.venv/bin/python backend/scripts/make_fixtures.py`; the JSON is
hand-written Met-shaped data and the images are generated solids, not provider
bytes.

## Failure handling

- Every failure is classified as `TransientFailure` or `PermanentFailure` with a
  short stable `reason` token, which is what lands in `jobs.last_error` and the
  logs. Exception messages, URLs with query strings, and bodies are not stored.
- A crash between the PUTs and the asset commit leaves objects with no asset
  row. The lease expires, the next attempt verifies and reuses those objects,
  and only then does an asset appear. If the job dies instead, the objects stay
  unreferenced; M1 does not garbage-collect them.
- A crash before the payload hint is written re-downloads once under the attempt
  cap. The content-addressed PUT overwrites identical bytes.

## Design decisions

- One provider whose policy defines the item flag, not a general public-domain
  heuristic. Ordinary public-domain status is not CC0 for this product.
- Duplicate works across sources are out of scope. Identical bytes under two
  source records share an object key and keep two asset rows, each with its own
  evidence. A later cross-source match is a relation between assets, not a merge.
- Separate per-process pools: the worker uses `WORKER_DATABASE_URL` (2 + 2
  overflow) and never borrows the API engine ([ADR-0004](../decisions/0004-host-processes.md)).

## Known limitations

- The public-address check resolves DNS separately from the HTTP client's own
  connection, so a DNS rebinding answer between the two is not caught. Network
  mode is opt-in and the image host allowlist still applies. Pinning the
  connection to the checked address is future work.
- The `Pacer` spaces requests within one process. Replicas would need a shared
  token bucket.
- An HTTP-date `Retry-After` falls back to computed backoff.
