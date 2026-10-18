# Evaluation

Status: board recommendation evaluation and latency benchmarks are implemented.
The search relevance benchmark in the
[MVP spec](mvp-spec.md#search-evaluation) is not implemented yet. M2 search is
covered by filter and ordering tests; see [search](systems/search.md) and
[lint and tests](lint-test.md). A cache for query embeddings is deferred until
repeated queries show up in latency.

The recommendation design is in [recommendations](systems/recommendations.md).
The reproducible real-image corpus is in the
[Met evaluation catalog](met-evaluation-catalog.md).

## Board recommendations

### Commands

Run from `backend/` with the local stack up. The embedding identity comes from
settings, and no model weights are loaded. To evaluate OpenCLIP vectors, set the
checkpoint:

```bash
EMBEDDING_PROVIDER=openclip MODEL_VERSION=laion2b_s34b_b79k \
  .venv/bin/python -m app.recommendations.evaluate \
  [--pool 20] [--k 10] [--retrievers centroid,per-pin,cluster] [--rankers cosine,mmr] \
  [--judgments-cohesive graded.csv] [--judgments-complement graded.csv] \
  [--export-human DIR]

EMBEDDING_PROVIDER=openclip MODEL_VERSION=laion2b_s34b_b79k \
  .venv/bin/python -m app.recommendations.benchmark --real --sizes 1000 10000
```

Output goes to `backend/evaluation/results/` (ignored by git) unless `--out`
says otherwise. `evaluate` writes `latest.json`, which is deterministic for a
fixed corpus, model, and board set, plus a separate `latest.latency.json`.
Running it twice gives identical results files. `benchmark` creates a
throwaway database per synthetic size, migrates it, fills it with READY
verified-CC0 rows and seeded random unit vectors, measures through `cc0_api`,
and drops it. `--real` only reads the configured catalog.

### Benchmark boards

`backend/evaluation/boards/v1.json` is versioned and hashed into every run
manifest. It defines nine boards by Met object id, all from the
[Met evaluation batch](systems/ingestion.md#local-real-image-evaluation-batch):

| Kind | Boards |
|---|---|
| single-pin | single-wheat-field |
| single-style | landscapes, self-portraits, still-lifes, seascapes |
| two-style | portraits-and-seascapes, still-lifes-and-architecture, gardens-and-landscapes |
| mixed | eclectic |

The interest labels (landscape, self-portrait, still-life, seascape, garden,
architecture, decorative-object) were assigned from Met titles by a maintainer.
They are **proxy labels** for coverage metrics, not relevance judgments.
Ambiguous objects are unlabeled.

### Method

1. **Retrieval, with no ranker.** For each retriever and board, the CLI
   retrieves the full pool, plus one leave-one-out pool per pin with that pin
   un-saved. Metrics:
   - held-out pool recall (does the removed pin come back?)
   - judged-relevant pool recall, when judgments exist
   - pool interest coverage (share of the board's labels present in the pool)
   - probe count and pool size
   - the relevance/novelty correlation of the pool

   Pools are kept in memory and listed per board in the results.
2. **Ranking, on those identical pools.** Every retriever × ranker pair is
   scored on the same cached pools, so differences between rankers are not
   caused by retrieval.
3. **Cohesive metrics:**
   - leave-one-out hit@k and MRR
   - nDCG@k against graded "fits this board" judgments
   - interest coverage in the top-k
4. **Complement metrics, reported separately.**
   - **Primary:** the mean human grade for "a good complement: compatible but
     adds something new".
   - **Diagnostics only:** novelty (distance to the nearest pin), intra-list
     diversity, and near-duplicate rate (≥ 0.95 cosine to a pin).
   - Leave-one-out recovery is not a complement metric, because it rewards
     near-duplicates.
5. **Shared:** catalog coverage across boards, and latency in the separate
   file.

The manifest records the board set version and hash, the model key/version,
corpus size and a hash of the sorted eligible ids, pool, k, every retriever
and ranker parameter, missing pins, and the judgment files.

### Human evaluation

`--export-human DIR` writes `cohesive-sheet.csv` and `complement-sheet.csv`.
Each sheet has one row per distinct item that the compared strategies
(`--compare-cohesive`, `--compare-complement`) put in a board's top-k. Rows
are shuffled by a hash and carry no strategy column. The matching
`*-key.json` maps each row to the strategies and ranks that produced it.

Judges fill `grade` with 0–3 for the sheet's own question:
- cohesive sheet: does it fit the board?
- complement sheet: is it a good complement?

Graded sheets are passed back with `--judgments-cohesive` /
`--judgments-complement`. Store graded sheets versioned next to the board file,
for example `backend/evaluation/judgments/cohesive-v1.csv`. **No judgments
exist yet**, so the nDCG and human-grade columns are empty in the results
below.

### Results: Met 1K catalog, OpenCLIP ViT-B-32 / laion2b_s34b_b79k

Snapshot: 1,010 eligible assets (the [Met evaluation catalog](met-evaluation-catalog.md)
plus the ten fixtures), corpus sha256 `8bbd1f49…`, board set v1 with no missing
pins, pool 20, k 10, 2026-10-18. With no human judgments yet, these numbers
check the pipeline and show the direction of effects. They are not evidence to
promote a strategy.

Retrieval (means over boards; recall excludes the single-pin board):

| Retriever | Held-out pool recall | Pool interest coverage | Relevance/novelty corr. |
|---|---|---|---|
| centroid | 0.292 | 0.574 | −0.518 |
| per-pin | 0.281 | 0.833 | **−1.000** |
| cluster | 0.375 | 0.574 | −0.586 |

Cohesive ranking (selected rows):

| Strategy | Hit@10 | MRR | Interest coverage@10 |
|---|---|---|---|
| centroid+cosine (default) | 0.177 | 0.049 | 0.407 |
| centroid+mmr | 0.167 | 0.022 | 0.407 |
| per-pin+round-robin | 0.187 | 0.053 | 0.519 |
| cluster+cosine | 0.198 | 0.059 | 0.296 |
| cluster+round-robin | 0.198 | 0.069 | 0.296 |

Findings:

- **Per-pin retrieval collapses relevance and novelty** (correlation −1.000),
  confirming that a weighted relevance + novelty score over per-pin probes is
  just similarity ranking.
- **Per-pin pools cover board interests best** (0.833 vs 0.574). Per-pin +
  round-robin keeps more of that coverage in the top 10 (0.519 vs 0.407), at
  about the same hit rate. That makes it the strongest candidate for mixed
  boards, pending human judgments.
- **Clustering is not reliable yet.** With the default thresholds it splits
  portraits-and-seascapes correctly but also splits the single-style
  self-portraits board. It does not split still-lifes-and-architecture or
  gardens-and-landscapes, and its top-10 interest coverage is the lowest. The
  thresholds need tuning on more boards.
- **Held-out recovery is low at this scale** (hit@10 ≈ 0.18–0.21 with 1K
  candidates). Leave-one-out is a strict proxy: a removed pin competes with
  many good neighbors.
- **`complement-baseline-v0` removed nothing:** the near-duplicate rate is 0 at
  0.95. It equals `centroid+cosine` here, as expected for a control.

An earlier run on the 57-asset batch (corpus `87deff4d…`) showed the same
direction for every finding.

### Latency

`python -m app.recommendations.benchmark` gives exact retrieval, a 6-pin board,
limit 20, and 30 timed runs after 3 warmups. "Query" is the candidate
statement alone; "path" is pins → probes → query → merge → rank. Measured on
2026-10-18 on the local Compose Postgres (WSL2):

| Corpus | Query, 1 probe P50/P95 | Query, 6 probes P50/P95 | centroid-cosine path P50/P95 | per-pin+cosine path P50/P95 |
|---|---|---|---|---|
| real Met 1K, 1,010 assets | 13 / 14 ms | 38 / 40 ms | 13 / 14 ms | 37 / 39 ms |
| synthetic 1,000 | 14 / 20 ms | 44 / 66 ms | 14 / 15 ms | 38 / 44 ms |
| synthetic 10,000 | 104 / 123 ms | 302 / 379 ms | 85 / 93 ms | 268 / 281 ms |

`EXPLAIN ANALYZE` at 10,000 assets:

- 1 probe: 98 ms
- 6 probes: 351 ms

Before the query fixes in [recommendations](systems/recommendations.md#candidate-query):

- 1 probe: 774 ms
- 6 probes: 5,119 ms

The remaining cost is linear in corpus size and in probe count:

- The rights view's per-row latest-evidence lookup costs about 40–50 ms at
  10K. It now runs once per statement.
- The exact distance scan costs about 40 ms per probe at 10K.

At the spec's ~10K target, the centroid default is under 100 ms P50. Multi-probe
retrievers cost roughly 3× that. Revisit an ANN index or a cheaper eligibility
projection if a multi-probe strategy is promoted.
