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
| `feat/recommendation-algo` | repo root | Recommendation engine, owned separately; do not modify |

## Dependencies

- Migration `0010_board_exports` currently revises `0008`; when the recommendation
  migration `0009` lands, repoint `down_revision` and rerun `tests/integration/test_migration.py`.

- Recommendation engine (`feat/recommendation-algo`) is uncommitted at the time of
  writing. Frontend integration and recommendation acceptance tests wait for it to land.

## Phase 1 — Exports
- [x] Schema + grants migration
- [x] Owner-scoped API (create/status/download)
- [x] Worker job: snapshot, rights recheck, ZIP, atomic publish
- [x] Retry / interruption / duplicate-execution safety
- [x] Lifecycle (expiry, cleanup) documented

## Phase 2 — Frontend
- [ ] Both recommendation modes on board page (built on `feat/recommendation-algo`, not mine; pending merge)
- [ ] Loading / empty / error / retry; stale-response guard (recommendations: pending merge; export: done, component tests)
- [x] Export action, progress, download, failure retry

## Phase 3 — Acceptance
- [x] Authorization and cross-user tests
- [x] Export retry/failure/rights tests; manifest vs. archive verification
- [x] E2E script (`backend/scripts/m4_e2e.py`, `test_m4_e2e.py`; green in a combined checkout, skipped without the rec engine)
- [x] Lint, typecheck, unit/integration, migration checks (per branch and combined)

## Phase 4 — Docs and review
- [x] mvp-spec status, export system doc, docs index, repro instructions
- [ ] evaluation.md results: owned by the recommendation branch; reproducibility re-run pending
- [x] Review pass and fixes (enqueue hardened; see git log)

## Merge recipe for `feat/recommendation-algo`

Conflicts are additive. Keep both sides in `backend/app/main.py` (routers),
`frontend/src/state/boards.tsx` (`getToken` and `recommend`), the board page
(`BoardRecommendations` and `BoardExport`), and `docs/README.md`. In
`backend/app/worker/__main__.py` keep that branch's `--follow` handling and add
this branch's `exports`, `exports-cleanup`, and export drain to `drain`. Repoint
`0010_board_exports.down_revision` to `0009_ready_assets_fail_closed` and confirm
`alembic heads` shows one head.
