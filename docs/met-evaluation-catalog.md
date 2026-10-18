# Met evaluation catalog

The versioned M4 dataset lives in
[`backend/evaluation/met-1k/v1`](../backend/evaluation/met-1k/v1). It uses real
Met images; the ten synthetic fixture images are excluded. All rights
decisions, image downloads, storage, and embeddings use the existing
[ingestion worker](systems/ingestion.md) and OpenCLIP job. The selection helper
only discovers and records Met object IDs. It does not change recommendation
ranking, which is documented separately in [evaluation](evaluation.md).

## Reproduce locally

Start PostgreSQL and RustFS, apply migrations, and install the backend
`embeddings` extra as described in [local development](local-development.md)
and [search](systems/search.md). Run from `backend/`:

```bash
EMBEDDING_PROVIDER=openclip MODEL_KEY=ViT-B-32 MODEL_VERSION=laion2b_s34b_b79k .venv/bin/python -m app.embeddings
.venv/bin/python scripts/met_eval.py select --per-category 50 --phase pilot
.venv/bin/python scripts/met_eval.py run --target 160 --batch-size 20
.venv/bin/python scripts/met_eval.py report --target 160
.venv/bin/python scripts/met_eval.py contact-sheet --limit-per-category 8
.venv/bin/python scripts/met_eval.py select --per-category 300 --phase expansion
.venv/bin/python scripts/met_eval.py run --target 1000 --batch-size 50
.venv/bin/python scripts/met_eval.py report --target 1000
```

The helper sets `INGEST_SOURCE_MODE=network`, `EMBEDDING_PROVIDER=openclip`,
`MODEL_KEY=ViT-B-32`, and `MODEL_VERSION=laion2b_s34b_b79k` for `run`; it does
not edit `.env`. Each batch invokes the existing `python -m app.worker ingest
--object-id ...` command. The worker enforces the item-level CC0 gate, image
host allowlist, download limits, one-request-per-second pacing, and durable
retry rules. Stop and rerun `run` at any point; the database owns job state.
To use this catalog for related works and board recommendations in the local
Compose API, set `CATALOG_EMBEDDING_MODEL_KEY=ViT-B-32` and
`CATALOG_EMBEDDING_MODEL_VERSION=laion2b_s34b_b79k` in `.env`, then recreate
the API container with `docker compose up --build -d api`. The default fake
inference provider can remain in the API; these two routes use persisted
vectors and do not run OpenCLIP. Semantic text search still requires the API
to run OpenCLIP with the matching checkpoint.
Inspect dead jobs with `python -m app.worker jobs --state dead`, and redrive
only after understanding the cause.
If a reused stored asset lacks this checkpoint's vector, run the existing
`reembed` command, then rerun the report. It queues only assets missing that
model identity:

```bash
EMBEDDING_PROVIDER=openclip MODEL_KEY=ViT-B-32 MODEL_VERSION=laion2b_s34b_b79k .venv/bin/python -m app.worker reembed
```

`select` calls the Met's paginated `/v1.1/search` endpoint and never downloads
image bytes. Its fixed terms, stable hash ordering, and observed OpenCLIP
weight-file SHA-256 are recorded in `selection.json`. `run` checks that cached
weight hash before ingestion. `candidates.jsonl` freezes selected IDs and
search terms; the helper reuses them on later runs. The endpoint and the Met's
rate guidance are documented in the
[official Collection API guide](https://metmuseum.github.io/).
`attempts.jsonl` records submitted IDs.
`baseline.json` records which assets and vectors existed before this run. The
checked-in `catalog.jsonl` freezes the final 1,000 IDs, categories, asset IDs,
checksums, object keys, and checkpoint. `report.json` records outcomes and
object sizes. A fresh local database uses its own state, not the checked-in
attempt ledger, to decide what to ingest. A later Met rights or image change
can prevent bit-for-bit replay; the manifest preserves the observed snapshot.

## Sampling and audit

The eight sampling targets are portraits/figures, landscapes/seascapes,
architecture/interiors, still lifes/botanicals, animals, textiles/garments,
sculpture/decorative objects, and patterns/designs. Five Met search terms per
target provide candidate IDs. Search hits do not establish image availability
or CC0 eligibility; only the worker's rights gate does that.

The final visual category is estimated from each existing OpenCLIP image
vector against fixed text prompts, without reembedding. The manifest retains
both the intended sampling category and estimated visual category because
automatic labels are approximate. Contact sheets draw a deterministic sample
from each visual group for human review. Thumbnail colour and aspect-ratio
summaries measure visual coverage;
they do not decide eligibility or recommendation rank.

`successful` means READY with latest verified CC0 evidence and a vector for
`ViT-B-32 / laion2b_s34b_b79k`. `rejected` means a metadata-only non-CC0 or
unknown rights decision. `failed` means a dead ingestion job. Reused and newly
stored/embedded counts are measured against the local pre-pilot baseline.
`report.json` sums the distinct original and thumbnail object sizes belonging
to the final catalog; RustFS metadata and filesystem overhead are excluded.

## Pilot result

The 160-image pilot reused 47 real Met assets and added 113 stored and embedded
assets. At the pilot gate, 94 candidate IDs had rights rejections and 2 had
dead ingestion jobs. Its selected originals occupied 355,662,793 logical bytes;
thumbnails occupied 4,732,406 bytes. The first contact sheet showed that Met
query and metadata labels can describe a different subject from the actual
image. In a preliminary OpenCLIP label check, 52 of 160 images had less than
0.015 cosine-score separation between their top two subject categories. Final
category counts therefore remain estimates, with the discrepancy reported.

## Final result

The 2026-10-18 snapshot contains **1,000 distinct real Met source IDs and
assets**. All 1,000 are visible through `ready_assets` and have a matching
`ViT-B-32 / laion2b_s34b_b79k` vector; no STORED or READY Met candidate lacks
that vector. There are no fixture IDs in the catalog. The worker's embed queue
had only succeeded jobs at the final check. The final `catalog.jsonl` SHA-256
is `46f64b7b66999a33b620beec9d1edd28d5841a56e0484b158b815ae3e2461b54`;
the candidate manifest SHA-256 is
`4667ec679b38cd53d048369c13835a5a5aaf98236c875fcc4eef2ffdc4c67fa5`.

| Measured outcome | Count |
|---|---:|
| Candidate IDs frozen | 2,447 |
| IDs submitted to ingestion | 1,565 |
| Successful READY and embedded | 1,000 |
| Reused stored assets and embeddings | 47 |
| Newly stored and embedded | 953 |
| Metadata-only non-CC0 rejections | 566 |
| Dead ingestion jobs | 46 |
| Ingest jobs needing more than one attempt | 1 |
| Unattempted reserve IDs | 835 |

The dead jobs were 42 source HTTP status failures, 3 over-size images, and 1
unsupported format. The final 1,000 originals occupy **1,822,567,702 bytes**
and thumbnails **25,391,216 bytes**, totaling **1,847,958,918 bytes** of
logical local object storage (about 1.72 GiB). Distinct object keys are counted
once and their stored checksum metadata was checked. Filesystem and RustFS
metadata overhead is outside this total.

The category counts below are estimates from the actual stored image vectors,
with a deterministic 64-image thumbnail sample inspected visually. The Met
sampling label attached to an ID is retained separately; it disagreed with the
visual estimate for 560 of 1,000 images. OpenCLIP's top two category scores
were within 0.015 for 289 images, so these counts are not human ground truth.

| Visual category | Images |
|---|---:|
| Portraits and figures | 68 |
| Landscapes and seascapes | 94 |
| Architecture and interiors | 89 |
| Still lifes and botanicals | 36 |
| Animals | 138 |
| Textiles and garments | 135 |
| Sculpture and decorative objects | 287 |
| Patterns and designs | 153 |

Aspect ratios are fairly spread: 324 wide, 313 tall, and 363 near-square.
Mean saturation is below 0.15 for 434 images. Warm yellow/red is the dominant
hue group for 802 images; saturated greens and blues are uncommon. The contact
sheet shows many sepia prints, drawings, and historical decorative objects.
Still lifes, portraits, vivid green/blue scenes, interiors, and modern abstract
work are relatively scarce. Animal-shaped objects, patterned textiles, and
architectural ornament sometimes straddle two categories. These gaps reflect
the Met collection, search-term sampling, CC0/image availability, and the
approximate OpenCLIP labels; they should be considered when interpreting M4
recommendation results.
