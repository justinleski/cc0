# M4 progress checklist

Persistent progress record for the M4 closeout (exports, frontend, acceptance).
Only tick an item once it is implemented **and** verified. See
[MVP spec §12](mvp-spec.md#12-implementation-order-and-acceptance-gates).

## Branch / worktree layout

| Branch | Worktree (`../cc0-worktrees/`) | Purpose |
| --- | --- | --- |
| `milestone/m4` | `milestone-m4` | M4 integration; feature branches merge here |
| `feat/m4-board-exports` | `m4-board-exports` | Phase 1: background exports |
| `feat/m4-frontend-integration` | `m4-frontend-integration` | Phase 2: board-page UI |
| `feat/m4-acceptance-evaluation` | `m4-acceptance-evaluation` | Phase 3/4: tests, E2E, docs |
| `milestone/m5` | `milestone-m5` | M5 integration (no work until requested) |
| `feat/m5-aws-infra`, `feat/m5-auth-cognito`, `feat/m5-ops-observability` | `m5-*` | M5 placeholders, empty |
| `feat/recommendation-algo` | repo root | Recommendation engine, owned separately; merged into `milestone/m4` |

## Dependencies

- Resolved: `0010_board_exports` revises `0009_ready_assets_fail_closed`; `alembic heads`
  shows one head (`0010_board_exports`).
- Resolved: `feat/recommendation-algo` (rights fix 0009, recommendation engine, worker
  `--follow`, stored embedding identity, Met 1K evaluation catalog) is merged.

## Phase 1 — Exports
- [x] Schema + grants migration
- [x] Owner-scoped API (create/status/download)
- [x] Worker job: snapshot, rights recheck, ZIP, atomic publish
- [x] Retry / interruption / duplicate-execution safety
- [x] Lifecycle (expiry, cleanup) documented

## Phase 2 — Frontend
- [x] Both recommendation modes on board page (from `feat/recommendation-algo`; rendered beside the export action)
- [x] Loading / empty / error / retry; stale-response guard (recommendations and export: component tests)
- [x] Export action, progress, download, failure retry

## Phase 3 — Acceptance
- [x] Authorization and cross-user tests
- [x] Export retry/failure/rights tests; manifest vs. archive verification
- [x] E2E script (`backend/scripts/m4_e2e.py`, `test_m4_e2e.py`; green in a combined checkout, skipped without the rec engine)
- [x] Lint, typecheck, unit/integration, migration checks (per branch and combined)

## Phase 4 — Docs and review
- [x] mvp-spec status, export system doc, docs index, repro instructions
- [ ] evaluation.md results: owned by the recommendation branch. Re-run check done: two consecutive
      `python -m app.recommendations.evaluate` runs on the local catalog wrote byte-identical results
      files (sha256 `6dbf3397…`). Published numbers there were not regenerated here.
- [x] Review pass and fixes (enqueue hardened; see git log)

## Merge recipe for `feat/recommendation-algo`

Conflicts are additive. Keep both sides in `backend/app/main.py` (routers),
`frontend/src/state/boards.tsx` (`getToken` and `recommend`), the board page
(`BoardRecommendations` and `BoardExport`), and `docs/README.md`. In
`backend/app/worker/__main__.py` keep that branch's `--follow` handling and add
this branch's `exports`, `exports-cleanup`, and export drain to `drain`. Repoint
`0010_board_exports.down_revision` to `0009_ready_assets_fail_closed` and confirm
`alembic heads` shows one head.

## Verification log (combined tree: M4 branches + uncommitted recommendation engine, 2026-10-18)

- Backend: `ruff check` clean; 259 unit tests; 69 integration tests (includes `test_exports.py`,
  `test_m4_e2e.py`, migration upgrade/downgrade); `alembic heads` shows one head.
- Frontend (Node 22): `tsc --noEmit`, `eslint .`, 39 vitest tests.
- `backend/scripts/m4_e2e.py`: 15 checks passed against real Postgres, RustFS, API and worker processes.
- Not done: real-browser (Playwright) run of the export UI; compose worker service (the worker is run by hand).

## Verification log (merge of `feat/recommendation-algo` into `milestone/m4`, 2026-10-18)

- `alembic heads`: one head, `0010_board_exports` (revises `0009_ready_assets_fail_closed`).
- Backend: `ruff check` clean; 261 unit tests; 71 integration tests, none skipped (includes
  `test_exports.py`, `test_m4_e2e.py`, `test_recommendations.py`, migration upgrade/downgrade).
- Frontend (Node 22): typecheck, lint, 39 vitest tests, production build.
- Not done: live `backend/scripts/m4_e2e.py` run (the local operator database was left at 0009);
  real-browser run of the combined board page; regenerating `evaluation.md` numbers.
