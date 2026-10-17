# Private boards and local authentication (M3 slice 2)

Status: Implemented backend. Related: [API](api.md), [storage](storage.md),
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

Revision 0007 adds `users`, `boards`, and `board_items`. UUID primary keys,
foreign keys, unique `(board_id, asset_id)`, nonnegative positions, and a
*deferrable* unique `(board_id, position)` constraint enforce persistent
invariants. The deferral permits atomic position swaps. There are indexes on
`boards(owner_user_id, created_at, id)` and `board_items(asset_id)`; the primary
key serves ordered per-board item reads. Name, description and note length
checks are also enforced in PostgreSQL. Deleting a board cascades its items.
The API role can select/insert users and fully manage boards and board items.
The worker has no access to these tables. No new asset or rights-table grants
were added.

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
| `POST /api/v1/boards` | Create from `{name, description?}`; 201 board |
| `GET /api/v1/boards?limit=50&offset=0` | Current user's summaries, newest first |
| `GET /api/v1/boards/{id}` | Board with visible ordered items |
| `PATCH /api/v1/boards/{id}` | Edit `name` and/or `description` |
| `DELETE /api/v1/boards/{id}` | Delete board and pins; 204 |
| `POST /api/v1/boards/{id}/items` | Pin `{asset_id, notes?}`; 201 board |
| `PATCH /api/v1/boards/{id}/items/{asset_id}` | Set `{notes}` |
| `DELETE /api/v1/boards/{id}/items/{asset_id}` | Remove pin |
| `PUT /api/v1/boards/{id}/items/order` | Set `{asset_ids: [...]}` for visible pins |

Board endpoints and errors send `Cache-Control: no-store`. There is no frontend
connection yet; the existing frontend board screen remains deferred.

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

Local bearer tokens are a development adapter. A production OIDC verifier and
ownership-aware frontend are later work. Position updates use a board-wide row
lock and issue one update per item; the pilot board size is small, so large-board
performance has not been measured.
