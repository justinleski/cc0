# Private boards and local authentication (M3 slices 2 and 3)

Status: Implemented backend and frontend. Related: [API](api.md), [storage](storage.md),
[search](search.md), [ADR-0003](../decisions/0003-database-roles.md),
[ADR-0005](../decisions/0005-feature-based-layered-architecture.md),
[MVP spec](../mvp-spec.md#8-boards-authentication-and-export-mvp).

## Local authentication

Board routes require `Authorization: Bearer <token>`. The default
`AUTH_MODE=disabled` returns 503 for every board request. To use local auth,
set `APP_ENV=local` (or `test`) explicitly, set `AUTH_MODE=local`, and supply a
private `AUTH_LOCAL_SIGNING_KEY` of at least 32 bytes. Generate a key once for
the local environment; the API and token CLI must read the same key. A signing
key is not committed in `.env.example`. Run `python -m app.auth token alice`
or `... bob` from the backend environment for a one-hour token. For example, after migration and API startup, add the two auth settings to
`.env` and generate a key with `openssl rand -hex 32`. Then run:

```bash
(cd backend && .venv/bin/python -m app.auth token alice)
# Supply the printed token as Authorization: Bearer <token> on /api/v1/boards.
```

The CLI is the
only token issuer; there is no public login or token-minting route.

PyJWT verifies the HS256 signature, expiry, issued-at time, issuer and audience.
Only `local:alice` and `local:bob` are accepted; each maps to a stable UUID.
The dependency inserts that verified subject into `users` once, then passes the
persisted ID to the board feature. Client-supplied user IDs in request bodies
are rejected. Requests without valid credentials return 401. The auth guard
also runs for every request; changing settings to production or disabling auth
returns 503 without opening a database connection. Startup rejects local auth
unless `APP_ENV` was explicitly set to `local` or `test`, and production still
requires the existing real-secret configuration. Production OIDC/JWT verification
is not implemented in this slice.

## Database and transactions

Revision 0007 adds `users`, `boards`, and `board_items`. Revision 0008 adds
`board_create_operations` for owner-scoped creation retries. UUID primary keys,
foreign keys, unique `(board_id, asset_id)`, nonnegative positions, and a
*deferrable* unique `(board_id, position)` constraint enforce persistent
invariants. The deferral permits atomic position swaps. There are indexes on
`boards(owner_user_id, created_at, id)` and `board_items(asset_id)`; the primary
key serves ordered per-board item reads. Name, description and note length
checks are also enforced in PostgreSQL. Deleting a board cascades its items.
The API role can select/insert users, select/insert creation operations, and
fully manage boards and board items. The worker has no access to these tables.
No new asset or rights-table grants were added.
Board creation accepts the original `{name, description?}` body. An optional
`Idempotency-Key` header (1–128 visible ASCII characters) identifies one
operation within the authenticated owner. The transaction reserves that key,
stores a SHA-256 fingerprint of the original name and description and a new
board UUID, then inserts the board. A concurrent retry waits for that transaction
and returns the same board. Reusing a key with a different original request
returns 409. The same key used by another owner creates an independent board.
Without a key, every request creates a board, including identical names. An
identical retry after an edit returns the current board with its original ID.
After deletion, the operation row remains and an identical retry returns 404
instead of recreating the board. Keys currently have no expiry or pruning.
The frontend keeps a key in session storage after an uncertain failure, reuses
it for the same form values, and clears it on success or a definitive
400/404/409/422 response; a distinct create action uses a new key. The pending
record also carries a session ID, which survives reload but changes on each
sign-in. Completion may clear only the matching session and operation key.
Responses from an earlier sign-in cannot alter the current session's boards,
authentication, errors, or pending creation key. The operation rows currently
have indefinite retention; pruning and monitoring remain an M4/M5 operational
consideration.

The implementation follows router → service → repository → queries. A repository
mutation begins one transaction, locks `boards` using both board ID and verified
owner ID, performs its write, updates `updated_at`, and reads the result before
commit. That row lock serializes concurrent additions, removals, note edits,
reorders and deletes on a board. A failure rolls back all writes. Item SQL also
includes an owner-scoped board join. Missing and foreign boards both return the
same 404 body, including item operations. Duplicate or unavailable pins and
stale reorder memberships return 409; invalid request shapes return 422 and
business validation errors return 400.

New pins are inserted via `INSERT ... SELECT` from `ready_assets`, so only an
asset that is READY with the latest verified CC0 evidence can be pinned. The
insert observes one database statement snapshot; if rights are withdrawn after
insertion, the pin row remains but public board responses omit it immediately.
No hidden asset metadata or object URL appears in board responses. Owners can
remove a hidden pin by its known ID. A reorder lists every currently visible
pin exactly once; hidden pins retain their rows and are placed after visible
pins in their prior relative order. Positions returned to clients are compact
0-based visible positions. When an asset is restored to `ready_assets`, its
saved pin and notes reappear. This policy preserves user data while satisfying
current publication rules on every read. It does not implement exports or
recommendations.

## HTTP contract

| Method and path | Result |
|---|---|
| `POST /api/v1/boards` | Create from `{name, description?}`; optional `Idempotency-Key` header; 201 board or identical retry, 409 for same-owner key reuse with different payload, 404 if the keyed board was deleted |
| `GET /api/v1/boards?limit=50&offset=0` | Current user's summaries, newest first |
| `GET /api/v1/boards/{id}` | Board with visible ordered items |
| `PATCH /api/v1/boards/{id}` | Edit `name` and/or `description` |
| `DELETE /api/v1/boards/{id}` | Delete board and pins; 204 |
| `POST /api/v1/boards/{id}/items` | Pin `{asset_id, notes?}`; 201 board |
| `PATCH /api/v1/boards/{id}/items/{asset_id}` | Set `{notes}` |
| `DELETE /api/v1/boards/{id}/items/{asset_id}` | Remove pin |
| `PUT /api/v1/boards/{id}/items/order` | Set `{asset_ids: [...]}` for visible pins |

Board endpoints and errors send `Cache-Control: no-store`.
When a board is deleted between list and detail requests, the frontend omits
that 404 from its loaded list. Authentication and service failures still surface.

## Frontend workflow (slice 3)

The Next.js app mounts `BoardProvider` in the root layout. `src/api/boards.ts`
is the typed client integration layer. Its same-origin `/api/boards` route
forwards authenticated requests to FastAPI with caching disabled. Board lists,
details and every mutation come from the API; session storage holds the
one-hour local bearer token, a session ID, and any pending creation key so a
reload can restore a safe retry. There are no seed boards or session-stored
pins. The UI decodes the token subject only to
select the local account label; FastAPI verifies the signature and owner on
every request. The backend CLI remains the only token issuer.

From Boards, select Alice or Bob and paste the corresponding CLI token. The
header switches accounts by clearing the token and all board state before a new
sign-in. Requests started under the prior identity cannot update the new state.
The provider fetches the paginated owner list and each board's visible pins,
then replaces state from API responses after mutations. Board operations are
serialized per board. Failed writes leave the current view unchanged; 404/409
responses trigger a read to reconcile with the database. A 401 clears the local
session. The UI shows loading, empty, service and authorization errors and
requires confirmation before deleting a board or removing a pin.

Asset detail pages provide save/remove controls. Board pages support metadata
editing, notes, earlier/later pin order, and pin removal. Board thumbnails and
titles use visibility-gated catalog endpoints. Browser rendering never decides
whether an asset is eligible or whether the user owns a board.

## Validation and remaining work

The full `./scripts/check.sh` run passed on 2026-10-16: 235 backend unit
tests, 48 real-database integration tests, 18 frontend tests, lint, typecheck,
and migration upgrade. The single warning is an existing Starlette/AnyIO
deprecation notice. Unit tests cover token forgery/claims, mode guards,
validation and SQL scoping.
Real PostgreSQL tests cover CRUD, two-user isolation, eligible and ineligible
pins, suppression, notes, reordering, duplicate races, simultaneous edits,
constraint failures, transaction rollback, and persistence across separate API lifespans. The migration test checks role
grants and upgrade/downgrade. Use `./scripts/check.sh` for the full check.

Local bearer tokens are a development adapter. A production OIDC verifier is
later work. Position updates use a board-wide row
lock and issue one update per item; the pilot board size is small, so large-board
performance has not been measured.

Slice 3 validation on 2026-10-16: the full `./scripts/check.sh` passed backend
lint, 235 unit tests, frontend typecheck/lint and 18 frontend tests, migration,
and 48 real-database integration tests. After a provider race regression test
was added, the final frontend run passed 19 tests and `npm run build`. Both
Playwright tests passed against the production frontend, authenticated FastAPI,
PostgreSQL and RustFS. The board browser workflow covered Alice sign-in,
real catalog search, board creation/edit/deletion, three eligible pins, note
edits, reordering, pin removal and re-addition, reload persistence, Bob account
switching, direct API owner denial, missing assets, an invalid token, and a
simulated failed browser request followed by a real retry. The browser test
deletes its board afterward. The isolated PostgreSQL board test
also rejects a genuinely stored but ineligible asset and checks rights
suppression. The only warning was the existing Starlette/AnyIO deprecation.

M3 remediation validation on 2026-10-17: `./scripts/check.sh` passed backend
lint, 242 unit tests, frontend typecheck/lint and 25 frontend tests, migration,
and 50 real-database integration tests. Board tests cover keyed and unkeyed
creation, owner isolation, edits, deletion, concurrent retries, rollback, and
retry across API lifespans. Search tests reject a cursor signed with the
published default database password.

Final F-002 race remediation on 2026-10-17: `./scripts/check.sh` passed backend
lint, 242 unit tests, frontend typecheck/lint and 29 frontend tests, migration,
and 50 real-database integration tests. Deferred frontend tests cover an old
409 arriving while a new account has a pending create, an uncertain failure and
retry with the same key, stale success after an account switch, and stale
success and 401 responses after signing back into the same account. A remount
test verifies the same session retains its pending key after an uncertain failure.
