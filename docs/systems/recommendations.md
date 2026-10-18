# Board recommendations (M4)

Status: Phase A baseline implemented, plus the retrieval/ranking experiment
framework and evaluation tooling. Related: [boards](boards.md),
[search](search.md), [storage](storage.md), [evaluation](../evaluation.md),
[ADR-0002](../decisions/0002-postgres-pgvector.md),
[ADR-0005](../decisions/0005-feature-based-layered-architecture.md),
[MVP spec §7](../mvp-spec.md#7-search-and-board-recommendations-mvp).

Recommendations are built in-house on the stored OpenCLIP vectors in pgvector.
There is no separate recommender service or store. A board's profile is
recomputed on every request from its current pins, so adding or removing a pin
changes the next response without a cache to invalidate.
The API selects the stored vector identity with
`CATALOG_EMBEDDING_MODEL_KEY` and `CATALOG_EMBEDDING_MODEL_VERSION`; if unset,
it uses the inference provider's identity. Recommendation requests only read
stored vectors and run pgvector queries. They do not load or run OpenCLIP in
the API process.

## Route

`GET /api/v1/boards/{id}/recommendations?mode=cohesive|complement&limit=20`

The route requires the same bearer token as the board routes. Another user's
board and a guessed id both return the same 404 body. `limit` is 1–50.

| Field | Meaning |
|---|---|
| `mode`, `strategy` | Requested mode and the configured strategy that served it. |
| `fallback` | `"browse"` when the board has no usable pins: newest public works, saved pins excluded, `score` null. |
| `profile_pin_count` | Pins that shaped the profile: public and embedded for the active model. |
| `ignored_pin_count` | Saved pins that did not: suppressed, rejected, or not embedded. |
| `board_version` | The board's `updated_at` from the same statement as the pins. The frontend drops responses older than the board it holds. |
| `items[]` | Asset cards plus `score`, `reasons` (`similar_to_pin`, `same_creator`, `same_style_tag`), and `closest_pin_id`. |

## Invariants

- **Eligibility.** Profiles and candidates come only from `ready_assets`
  ([search](search.md#what-a-row-means)). That covers rights withdrawal, later
  rejections, and the 0009 fail-closed tie rule.
- **Exclusion.** `BOARD_PINS` returns every saved pin, eligible or not,
  starting from `board_items` like boards' `ALL_ITEMS`. `BoardPins.saved_ids`
  is the only source of the exclusion list. It is passed to every candidate
  query and re-checked after merging. A pin that is suppressed and later
  restored is therefore never recommended back while it is saved.
- **Profile.** Only `BoardPins.profile_pins` (eligible pins with an
  active-model vector) shape retrieval.
- **Determinism.** There is no randomness. Every sort breaks ties on asset id,
  and probes do not depend on board position.
- **Strategy from config only.** `RECOMMENDER_COHESIVE_STRATEGY` and
  `RECOMMENDER_COMPLEMENT_STRATEGY` must name entries in
  `strategy.STRATEGIES`, and `create_app` rejects unknown names at startup.
  Requests cannot choose a strategy.

## Layers

| Module | Role |
|---|---|
| `queries.py` | `BOARD_PINS` (owner-scoped board, pins, eligibility, vectors in one statement), `CANDIDATES` (exact multi-probe nearest neighbors), and evaluation-only `PINS_BY_ID` / `CORPUS_IDS`. |
| `repository.py` | Runs the queries as `cc0_api` and maps rows; raises `BoardNotFound` when the owner-scoped board is missing. |
| `retrieve.py` | `Retriever`s turn profile pins into `Probe`s (vector, pins, budget). `merge` dedupes hits into a `CandidatePool` and records every candidate's similarity to every probe and every pin. |
| `rank.py` | `Ranker`s order a fixed pool. |
| `strategy.py` | Named retriever + ranker pairings; the `RETRIEVERS` × `RANKERS` grid for evaluation. |
| `service.py` | pins → probes → candidates → merge → rank → reasons; browse fallback. `ranked()` is shared with the evaluation CLI. |
| `metrics.py`, `evaluate.py`, `benchmark.py` | Offline evaluation and latency measurement; see [evaluation](../evaluation.md). |

Retrieval decides which candidates are fetched, and ranking only orders a pool
it is given. Keeping the two apart lets evaluation score every ranker on
identical pools.

### Candidate query

`CANDIDATES` sends all probes in one statement (`unnest` of probe literals and
per-probe budgets, then a `LATERAL` top-k per probe). Three choices came from
`EXPLAIN ANALYZE`, and the [evaluation](../evaluation.md#latency) has the
numbers:

1. **`probe` is `MATERIALIZED`.** If inlined, Postgres re-parses the probe's
   512-float text literal into a vector for every candidate row. That cost
   more than the scan itself.
2. **`eligible` is `MATERIALIZED`.** The rights view (a per-row latest-evidence
   lookup) runs once per statement, not once per probe.
3. **The top-k scan selects only ids and distances.** Postgres projects the
   select list before the sort, so `embedding::text` and card columns are read
   for the k winners only.

Retrieval is exact. No HNSW or IVFFlat index exists; add one only if
measurements require it.

## Strategies

| Name | Retriever | Ranker | Status |
|---|---|---|---|
| `centroid-cosine` | `centroid`: one probe, the L2-normalized mean (a single pin is its own vector) | `cosine` | **Cohesive default.** A single-pin board returns exactly `/assets/{id}/related` order. |
| `complement-baseline-v0` | `centroid`, pool 100 | `complement-baseline-v0`: drop candidates with cosine ≥ 0.95 to any pin, then cosine | **Complement default, and an experimental control only.** It is not a definition of complementarity. On the current catalog it removes nothing and equals cosine. |

The grid (`grid_strategy(retriever, ranker)`) is for evaluation. None of these
are defaults:

- **Retrievers.**
  - `per-pin` gives every pin a probe with an equal budget,
    `max(floor, ceil(pool / pins))`, with no cap based on board position.
  - `cluster` / `cluster-proportional` use deterministic spherical k-means
    (farthest-first init, ids sorted first).
    - k stays 1 unless some k ≤ 4 passes all three gates: silhouette ≥ 0.15,
      every cluster has at least 2 pins, and every pair of cluster centroids is
      at least 0.15 cosine distance apart.
    - The separation gate exists because silhouette doesn't depend on scale:
      one tight style split in half still scores well.
    - All three thresholds are untuned defaults.
- **Rankers.**
  - `cosine` scores by best-probe similarity.
  - `mmr` gives diversity among the selected results: `trade_off` (0.7) ×
    relevance, minus the rest × max similarity to already chosen results.
  - `round-robin` interleaves probes so each interest gets a turn.

### Complement is an open problem

Diversity among results (MMR) is not complementarity. A naive "relevance +
novelty" score fails whenever both terms measure the same thing. With
`per-pin`, relevance (similarity to the best pin probe) and novelty (distance
to the nearest pin) are exact inverses, so any weighted sum is ordinary
similarity ranking or its reverse. The evaluation reports this correlation
(−1.000 for `per-pin`).

Hypotheses to test against `complement-baseline-v0`:

1. Relevance measured against the board profile and novelty against the
   nearest single pin, with a relevance floor. Check empirically that the two
   terms aren't strongly correlated.
2. Attribute contrast: shared style but different subject or composition.
3. Coverage objectives, or role-aware and aesthetic models.

A hypothesis replaces the baseline only if it wins on **human complement
judgments**. Offline proxies are not enough.

## Frontend

`src/components/BoardRecommendations.tsx` is the rail on the board page. Its
"More like this board" / "Complement (experimental)" toggle switches mode.
`useBoards().recommend` handles the request and its 401 handling.

Stale responses never render:

- Each request gets a sequence number, and changing pins or mode aborts the
  previous request.
- A response is applied only if it is the latest request.
- If `board_version` is older than the board the page holds, the rail refetches
  rather than showing it.
- Refetches follow pin membership. Notes and order don't change the profile, so
  they don't refetch.

## Deferred

Recency weighting, negative feedback ("less like this"), CLIP text steering,
an interaction event log, and collaborative signals are all deferred until the
evaluation framework shows the baseline's limits.
