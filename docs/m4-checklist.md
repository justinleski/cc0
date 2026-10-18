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
- [ ] Both recommendation modes on board page
- [ ] Loading / empty / error / retry; stale-response guard
- [ ] Export action, progress, download, failure retry

## Phase 3 — Acceptance
- [ ] Authorization and cross-user tests
- [ ] Export retry/failure/rights tests; manifest vs. archive verification
- [ ] E2E script
- [ ] Lint, typecheck, unit/integration, migration checks

## Phase 4 — Docs and review
- [ ] mvp-spec, evaluation, export system doc, docs index, repro instructions
- [ ] Review pass and fixes
