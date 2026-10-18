# Board exports (M4)

Status: backend implemented; frontend integration is tracked in
[the M4 checklist](../m4-checklist.md). Related: [boards](boards.md),
[ingestion](ingestion.md) (job framework), [storage](storage.md),
[ADR-0003](../decisions/0003-database-roles.md),
[MVP spec §8](../mvp-spec.md#8-boards-authentication-and-export-mvp).

An export is a ZIP of the originals pinned to a board, with `manifest.json` and
`attribution.csv`. It is built by the existing worker and job table, not by a
separate service or queue.

## API

All routes require the same bearer token as boards and are scoped to the
owner. An unknown ID and another user's ID both return 404.

| Route | Result |
| --- | --- |
| `POST /api/v1/boards/{id}/exports` | `202` with the export (`QUEUED`). `409` if no pin is currently exportable, `400` above `EXPORT_MAX_ASSETS`, `429` at `EXPORT_MAX_ACTIVE_PER_OWNER` queued exports. |
| `GET /api/v1/exports/{id}` | Status. When `READY` and every packaged asset is still eligible, includes `download_url` (presigned, `EXPORT_DOWNLOAD_URL_SECONDS`, default 60) and `download_expires_in`. |
| `GET /api/v1/boards/{id}/exports` | The board's recent exports, newest first, never with URLs. The UI uses it to resume after a reload. |

Statuses: `QUEUED` (waiting or retrying), `READY`, `FAILED` (`failure_reason`
is a short token such as `no_eligible_assets`, `object_store`,
`rights_changed`), `EXPIRED`.

Each `POST` creates a new export; there is no implicit reuse of an earlier one,
so a user who edited the board always gets a fresh snapshot.

## Job lifecycle

1. **Request.** One API transaction locks the board row (the lock every board
   mutation takes), snapshots the pins that are `READY` with verified CC0
   evidence into `board_export_items`, inserts the `board_exports` row, and
   creates the `jobs` row (`kind = export`, `dedupe_key = export:<id>`). A
   committed export therefore always has a job. The API role has no privilege
   on `jobs`; it may only `EXECUTE` the `cc0_enqueue_export_job` SECURITY
   DEFINER function, which inserts the single job a `QUEUED` export needs.
2. **Claim.** `python -m app.worker exports` (also run by `drain`) claims one job
   with the normal lease, claim token, and attempt counting. A `LeaseKeeper`
   renews the lease while packaging.
3. **Package.** The worker reads the snapshot with current rights evidence,
   excludes anything no longer eligible, reads each original from object storage
   and checks its SHA-256 against `assets.sha256`, and writes the ZIP to a local
   temp file. Entries are in position order with a fixed timestamp, so identical
   input gives identical bytes.
4. **Upload.** The file is uploaded to
   `exports/<owner>/<export>/<archive-sha256>.zip` (S3 multipart objects appear
   only when complete; the length is verified afterwards).
5. **Publish.** One transaction, fenced by the claim token: lock the asset rows,
   recheck eligibility, mark `board_exports` `READY` with the key, digest, size,
   counts and `expires_at`, and mark the job `succeeded`.

The worker has no access to `boards` or `board_items`. The snapshot is the
contract: edits to the board after the `POST` do not change that export.

## Retry, failure, and duplicate execution

- Object-store outages and other transient failures use the shared backoff and
  retry budget. After the last attempt the export is `FAILED` (`object_store` etc.)
  and the job is `dead`; `python -m app.worker redrive` returns both to the queue.
- A worker that dies leaves a leased job; the lease expires, another worker
  reclaims it, and the attempt is counted. A job that was dead-lettered by the
  reaper is reflected on the export row by the sweep that runs at every drain.
- Nothing is published until the final transaction, so a crash at any earlier
  point leaves `QUEUED` plus at most an unreferenced object. The key contains the
  archive digest, so a retry with different content never overwrites an object a
  reader might hold; identical content converges on the same key.
- A stale claim (duplicate delivery, expired lease) cannot publish: the fenced
  transaction raises `ClaimLost`. A job whose export already left `QUEUED` is
  closed without touching it.
- Permanent outcomes (`no_eligible_assets`, missing export, bad payload) fail the
  export and the job in one transaction.

## Rights consistency

Eligibility is the `ready_assets` rule: asset `READY` and the newest
`rights_evidence` row verified CC0. It is checked at four points:

1. at request time (only eligible pins are snapshotted);
2. at packaging (an asset that became ineligible is left out and listed in
   `manifest.json` under `excluded` with reason `rights_changed`; unreadable or
   digest-mismatched originals are excluded as `original_missing` /
   `original_corrupt`; if nothing remains the export fails);
3. at publish, after taking `FOR SHARE` locks on the asset rows. If a packaged
   asset lost eligibility meanwhile, nothing is published and the job retries,
   which rebuilds without it;
4. at every status read. A `READY` export containing an asset that is no longer
   eligible is reported `FAILED / rights_changed` and never receives a URL.

The remaining window is a rights decision committing after check 4 and before
the browser uses a URL that lives at most one minute. `exports-cleanup`
retires such archives and deletes the object.

## Storage lifecycle

- Archives live under the private `exports/` prefix of the existing bucket; the
  bucket is not public. Download is a presigned GET. When the API runs in a
  container, set `OBJECT_STORE_PUBLIC_ENDPOINT` to the host URL the browser can
  reach so the signature matches.
- `EXPORT_TTL_HOURS` (default 24) sets `expires_at`. After that the API reports
  `EXPIRED` immediately; `python -m app.worker exports-cleanup` deletes the object
  and records `EXPIRED`. Run it periodically (cron or a loop). On AWS an S3
  lifecycle rule on `exports/` is the backstop (M5).
- The same command removes archives whose assets lost eligibility and any object
  under `exports/` that no row references and is older than
  `EXPORT_ORPHAN_GRACE_HOURS` (default 2), which covers uploads from crashed or
  rejected attempts. Deleting an object happens before the row changes, so a
  failed delete is retried.
- Deleting a board keeps its export rows (`board_id` becomes null) so cleanup
  still finds the objects. The worker role has no `DELETE`; rows are history.

## Archive contents

```
images/001-<asset-id>.<ext>   originals, position order, names derived by the worker
attribution.csv               asset_id, filename, title, creator, source,
                              source_landing_url, original_uri, rights_type,
                              license_evidence_url, fetched_at, verified_at, exported_at
manifest.json                 manifest_version, export_id, board, exported_at,
                              assets[] (sha256, dimensions, source, rights evidence
                              with method/statement/verified_at), excluded[]
```

`exported_at` is the request time, which keeps retried archives byte-identical.
CSV cells that begin with `=`, `+`, `-` or `@` are prefixed with `'`.

## Operating it

```bash
cd backend
.venv/bin/python -m app.worker exports            # run queued exports
.venv/bin/python -m app.worker exports-cleanup    # retire expired/withdrawn, sweep orphans
.venv/bin/python -m app.worker jobs --state dead  # inspect failures
```

Tests: `backend/tests/integration/test_exports.py` (ownership, contents against
the catalog, rights suppression before and after packaging, interrupted upload,
attempt exhaustion and redrive, duplicate claims, expiry and orphan cleanup,
role privileges) and `backend/tests/unit/test_export_packager.py`.
