"""Select and audit a reproducible Met evaluation catalog.

Selection only discovers object IDs. All rights decisions, image downloads,
storage, retries, and embeddings remain in ``app.worker``.
Run from ``backend/`` with ``.venv/bin/python scripts/met_eval.py ...``.
"""

import argparse
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.embeddings.openclip import OpenClipProvider
from app.ingestion.errors import TransientFailure
from app.ingestion.fetch import HttpJsonSource, Pacer
from app.integrations.object_store import create_s3_client

ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "met-1k" / "v1"
MODEL_KEY = "ViT-B-32"
MODEL_VERSION = "laion2b_s34b_b79k"
# SHA-256 of the cached safetensors file used for the observed v1 corpus.
CHECKPOINT_SHA256 = "ac4f8c4b88af6d963118cbf40ad93176d092abbedfcb752601ae1866352656e6"
SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
EXCLUDED_FIXTURES = {str(value) for value in range(100001, 100011)}
CATEGORIES = {
    "portraits_figures": ("portrait", "figure", "woman", "man", "child"),
    "landscapes_seascapes": ("landscape", "seascape", "mountain", "river", "garden"),
    "architecture_interiors": ("architecture", "building", "interior", "temple", "cathedral"),
    "still_lifes_botanicals": ("still life", "flower", "fruit", "botanical", "bouquet"),
    "animals": ("bird", "horse", "dog", "cat", "animal"),
    "textiles_garments": ("textile", "garment", "dress", "robe", "fabric"),
    "sculpture_objects": ("sculpture", "ceramic", "vessel", "furniture", "jewelry"),
    "patterns_designs": ("pattern", "ornament", "geometric", "abstract", "design"),
}
VISUAL_PROMPTS = {
    "portraits_figures": (
        "a portrait of a person", "a painting of people", "a drawing of a human figure"),
    "landscapes_seascapes": (
        "a landscape with mountains and fields", "a seascape with water and boats",
        "a scenic view of a garden or river"),
    "architecture_interiors": (
        "a picture of a building facade", "an interior room with architecture",
        "an architectural drawing of a temple"),
    "still_lifes_botanicals": (
        "a still life painting of flowers or fruit", "a botanical illustration of plants",
        "an arrangement of inanimate objects"),
    "animals": (
        "an artwork depicting an animal", "a sculpture of a bird or horse",
        "a picture of wildlife"),
    "textiles_garments": (
        "a textile fabric or rug", "a garment or dress on display", "embroidered cloth"),
    "sculpture_objects": (
        "a ceramic pot or decorative object", "a sculpture or statue",
        "a piece of furniture or jewelry"),
    "patterns_designs": (
        "an abstract pattern or design", "a geometric ornament",
        "a repeating decorative motif"),
}


def _audit_engine(settings):
    # The worker role intentionally cannot select ready_assets; the local
    # migrator login reads the private rights ledger for this offline audit.
    url = make_url(settings.migration_database_url).set(drivername="postgresql+psycopg")
    return create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=True)


def _verify_checkpoint(settings):
    cache = settings.embedding_cache_path / "blobs"
    for path in cache.rglob("*") if cache.exists() else ():
        if not path.is_file() or path.stat().st_size < 100_000_000:
            continue
        with path.open("rb") as checkpoint:
            digest = hashlib.file_digest(checkpoint, "sha256").hexdigest()
        if digest == CHECKPOINT_SHA256:
            return
    raise SystemExit("Expected OpenCLIP weights not found in cache; run the smoke and check "
                     "the checkpoint SHA-256 before ingesting")


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _db_rows(ids):
    if not ids:
        return {}
    settings = get_settings()
    engine = _audit_engine(settings)
    query = text("""
        SELECT s.external_id, s.eligibility_status, s.raw_metadata,
               a.id::text AS asset_id, a.status AS asset_status, a.title,
               a.original_key, a.thumbnail_key, a.sha256, a.width, a.height,
               a.ingested_at, e.generated_at AS embedded_at,
               (r.id IS NOT NULL) AS verified_ready,
               j.state AS job_state, j.last_error, j.attempt_count
        FROM source_records s
        JOIN providers p ON p.id = s.provider_id AND p.name = 'met'
        LEFT JOIN assets a ON a.source_record_id = s.id
        LEFT JOIN ready_assets r ON r.id = a.id
        LEFT JOIN asset_embeddings e ON e.asset_id = a.id
            AND e.model_key = :model_key AND e.model_version = :model_version
        LEFT JOIN jobs j ON j.dedupe_key = 'ingest:met:' || s.external_id
        WHERE s.external_id = ANY(:ids)
    """)
    try:
        with engine.connect() as connection:
            rows = connection.execute(query, {
                "ids": ids, "model_key": MODEL_KEY, "model_version": MODEL_VERSION,
            }).mappings().all()
        return {row["external_id"]: dict(row) for row in rows}
    finally:
        engine.dispose()


def _category_from_metadata(row):
    metadata = row.get("raw_metadata") or {}
    words = " ".join(str(metadata.get(key) or "") for key in ("title", "classification", "medium"))
    words = words.casefold()
    for category, queries in CATEGORIES.items():
        if any(query in words for query in queries):
            return category
    return "sculpture_objects"


def _search(source, query):
    url = SEARCH_URL + "?" + urlencode({"q": query, "hasImages": "true", "limit": 500})
    for attempt in range(4):
        try:
            payload = source.get_json(url)
            ids = payload.get("objectIDs") if isinstance(payload, dict) else None
            if not isinstance(ids, list):
                return []
            return [str(value) for value in ids if isinstance(value, int)]
        except TransientFailure as error:
            if attempt == 3:
                raise
            time.sleep(error.retry_after or min(2 ** attempt, 8))
    return []


def select(per_category, phase):
    ROOT.mkdir(parents=True, exist_ok=True)
    selection_path = ROOT / "selection.json"
    if not selection_path.exists():
        selection_path.write_text(json.dumps({
            "version": 1,
            "source": "The Metropolitan Museum of Art Collection API",
            "endpoint": SEARCH_URL,
            "manifest_recorded_at": datetime.now(UTC).isoformat(),
            "ordering": "sha256(met-1k-v1:category:query:source_id)",
            "queries": CATEGORIES,
            "model_key": MODEL_KEY,
            "model_version": MODEL_VERSION,
            "checkpoint_sha256": CHECKPOINT_SHA256,
            "excluded_fixture_ids": sorted(EXCLUDED_FIXTURES),
        }, indent=2, sort_keys=True) + "\n")
    path = ROOT / "candidates.jsonl"
    rows = _read_jsonl(path)
    seen = {row["source_id"] for row in rows}
    settings = get_settings()
    engine = _audit_engine(settings)
    try:
        with engine.connect() as connection:
            prior = connection.execute(text("""
                SELECT s.external_id, s.raw_metadata
                FROM assets a JOIN source_records s ON s.id = a.source_record_id
                JOIN providers p ON p.id = s.provider_id AND p.name = 'met'
                JOIN ready_assets r ON r.id = a.id
                JOIN asset_embeddings e ON e.asset_id = a.id
                  AND e.model_key = :key AND e.model_version = :version
                ORDER BY s.external_id
            """), {"key": MODEL_KEY, "version": MODEL_VERSION}).mappings().all()
    finally:
        engine.dispose()
    for row in prior:
        source_id = row["external_id"]
        if source_id in seen or source_id in EXCLUDED_FIXTURES:
            continue
        rows.append({"source_id": source_id, "category": _category_from_metadata(row),
                     "query": None, "phase": "reused", "rank": None})
        seen.add(source_id)

    source = HttpJsonSource(settings.metadata_host_allowlist,
                            timeout_seconds=settings.ingest_request_timeout_seconds,
                            pacer=Pacer(settings.ingest_rate_limit_rps))
    try:
        for category, queries in CATEGORIES.items():
            count = sum(row["category"] == category for row in rows)
            per_query = math.ceil(per_category / len(queries))
            for query in queries:
                query_count = sum(row["category"] == category and row["query"] == query
                                  for row in rows)
                if query_count >= per_query:
                    continue
                ids = _search(source, query)
                # Stable spread through the result set; avoid only the most popular works.
                ordered = sorted(enumerate(ids), key=lambda pair: hashlib.sha256(
                    f"met-1k-v1:{category}:{query}:{pair[1]}".encode()).digest())
                for rank, source_id in ordered:
                    if query_count >= per_query:
                        break
                    if source_id in seen or source_id in EXCLUDED_FIXTURES:
                        continue
                    rows.append({"source_id": source_id, "category": category,
                                 "query": query, "phase": phase, "rank": rank})
                    seen.add(source_id)
                    count += 1
                    query_count += 1
            print(f"{category}: {count}/{per_category}", flush=True)
            _write_jsonl(path, rows)
    finally:
        source.close()
    _write_jsonl(path, rows)
    print(f"Saved {len(rows)} unique candidates to {path}")


def _eligible(row):
    return bool(row and row["verified_ready"] and row["embedded_at"])


def _visual_labels(ids):
    """Estimate visible subject from existing image vectors, without reembedding."""
    if not ids:
        return {}
    settings = get_settings()
    engine = _audit_engine(settings)
    try:
        with engine.connect() as connection:
            vectors = connection.execute(text("""
                SELECT s.external_id, e.embedding::text AS vector
                FROM source_records s JOIN assets a ON a.source_record_id = s.id
                JOIN asset_embeddings e ON e.asset_id = a.id
                WHERE s.external_id = ANY(:ids)
                  AND e.model_key = :key AND e.model_version = :version
            """), {"ids": ids, "key": MODEL_KEY, "version": MODEL_VERSION}).mappings().all()
    finally:
        engine.dispose()
    provider = OpenClipProvider(model_key=MODEL_KEY, model_version=MODEL_VERSION,
                                dimensions=512, cache_dir=settings.embedding_cache_path)
    prompts = [(category, prompt) for category, values in VISUAL_PROMPTS.items()
               for prompt in values]
    text_vectors = np.asarray([provider.embed_text(prompt) for _, prompt in prompts],
                              dtype=np.float32)
    labels = {}
    for row in vectors:
        image_vector = np.asarray(json.loads(row["vector"]), dtype=np.float32)
        similarity = text_vectors @ image_vector
        scores = {category: float(max(similarity[index] for index, pair in enumerate(prompts)
                                      if pair[0] == category)) for category in CATEGORIES}
        ranked = sorted(scores, key=lambda category: (-scores[category], category))
        labels[row["external_id"]] = (ranked[0], round(scores[ranked[0]] - scores[ranked[1]], 4))
    return labels


def _visual_metrics(body, width, height):
    with Image.open(io.BytesIO(body)) as image:
        pixels = np.asarray(image.convert("HSV").resize((64, 64)), dtype=np.float32)
    hue, saturation, brightness = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    colourful = saturation > 50
    hue_bin = None
    if np.any(colourful):
        # Pillow HSV hue spans 0..255. Fixed bins make reports comparable.
        bins = np.histogram((hue[colourful] + 256 / 12) % 256,
                            bins=6, range=(0, 256))[0]
        hue_bin = ("red", "yellow", "green", "cyan", "blue", "magenta")[int(bins.argmax())]
    ratio = round(width / height, 3)
    return {"aspect_ratio": ratio,
            "composition": "wide" if ratio > 1.3 else "tall" if ratio < 0.77 else "near_square",
            "mean_saturation": round(float(saturation.mean() / 255), 3),
            "mean_brightness": round(float(brightness.mean() / 255), 3),
            "dominant_hue": hue_bin}


def run(target, batch_size):
    rows = _read_jsonl(ROOT / "candidates.jsonl")
    if not rows:
        raise SystemExit("No candidates; run select first")
    if batch_size > 100:
        raise SystemExit("Batch size must be <= 100")
    _verify_checkpoint(get_settings())
    before_path = ROOT / "baseline.json"
    ids = [row["source_id"] for row in rows]
    snapshot = _db_rows(ids)
    observed_ready = {source_id for source_id, item in snapshot.items() if _eligible(item)}
    old_baseline = json.loads(before_path.read_text()) if before_path.exists() else {}
    # A checked-in ledger may be replayed against an empty local database.
    # Its prior baseline must not become this run's reuse count.
    reset_baseline = not set(old_baseline.get("ready_ids", [])).issubset(observed_ready)
    if not before_path.exists() or reset_baseline:
        baseline = {"recorded_at": datetime.now(UTC).isoformat(),
                    "ready_ids": sorted(source_id for source_id, item in snapshot.items()
                                        if _eligible(item)),
                    "stored_ids": sorted(source_id for source_id, item in snapshot.items()
                                         if item["asset_id"]),
                    "embedded_ids": sorted(source_id for source_id, item in snapshot.items()
                                           if item["embedded_at"])}
        before_path.write_text(json.dumps(baseline, indent=2) + "\n")
    attempts_path = ROOT / "attempts.jsonl"
    attempts = _read_jsonl(attempts_path)
    environment = os.environ.copy()
    environment.update({"INGEST_SOURCE_MODE": "network", "EMBEDDING_PROVIDER": "openclip",
                        "MODEL_KEY": MODEL_KEY, "MODEL_VERSION": MODEL_VERSION})
    while True:
        snapshot = _db_rows(ids)
        ready = [item for item in rows if _eligible(snapshot.get(item["source_id"]))]
        print(f"READY with active embedding: {len(ready)}/{target}", flush=True)
        if len(ready) >= target:
            return
        counts = Counter(item["category"] for item in ready)
        available = [item for item in rows if not _eligible(snapshot.get(item["source_id"]))
                     and (snapshot.get(item["source_id"]) or {}).get("job_state") is None
                     and (snapshot.get(item["source_id"]) or {}).get("eligibility_status")
                     not in ("NON_CC0_CACHED", "UNKNOWN_CACHED")]
        if not available:
            raise SystemExit("Candidate list exhausted; add more with select and resume")
        available.sort(key=lambda item: (counts[item["category"]],
                                         list(CATEGORIES).index(item["category"])))
        batch = available[:min(batch_size, target - len(ready))]
        command = [sys.executable, "-m", "app.worker", "ingest"]
        for item in batch:
            command.extend(["--object-id", item["source_id"]])
        stamp = datetime.now(UTC).isoformat()
        for item in batch:
            attempts.append({"source_id": item["source_id"], "category": item["category"],
                             "started_at": stamp})
        _write_jsonl(attempts_path, attempts)
        print(f"Running {len(batch)} IDs; first={batch[0]['source_id']}", flush=True)
        result = subprocess.run(command, env=environment, check=False)
        print(f"Worker exit={result.returncode}", flush=True)
        if result.returncode not in (0, 1):
            raise SystemExit("Worker failed unexpectedly; inspect logs and resume")


def report(target):
    candidates = _read_jsonl(ROOT / "candidates.jsonl")
    baseline_path = ROOT / "baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    rows = _db_rows([item["source_id"] for item in candidates])
    qualifying = [(item, rows[item["source_id"]]) for item in candidates
                  if _eligible(rows.get(item["source_id"]))]
    visual_labels = _visual_labels([item["source_id"] for item, _ in qualifying])
    # Keep one frozen, deterministic set of exactly target assets when enough exist.
    by_category = {category: [pair for pair in qualifying
                              if visual_labels[pair[0]["source_id"]][0] == category]
                   for category in CATEGORIES}
    selected = []
    while len(selected) < target and any(by_category.values()):
        for category in CATEGORIES:
            if len(selected) >= target:
                break
            if by_category[category]:
                selected.append(by_category[category].pop(0))
    output = []
    for item, row in selected:
        visual_category, margin = visual_labels[item["source_id"]]
        output.append({"source_id": item["source_id"], "category": visual_category,
                       "sampling_category": item["category"], "visual_score_margin": margin,
                       "asset_id": row["asset_id"], "sha256": row["sha256"],
                       "original_key": row["original_key"],
                       "thumbnail_key": row["thumbnail_key"],
                       "model_key": MODEL_KEY, "model_version": MODEL_VERSION})
    _write_jsonl(ROOT / "catalog.jsonl", output)
    counts = Counter(item["category"] for item in output)
    outcomes = Counter()
    attempted_ids = {item["source_id"] for item in _read_jsonl(ROOT / "attempts.jsonl")}
    for item in candidates:
        row = rows.get(item["source_id"])
        if _eligible(row):
            outcomes["successful"] += 1
        elif row and row["eligibility_status"] in ("NON_CC0_CACHED", "UNKNOWN_CACHED"):
            outcomes["rejected"] += 1
        elif row and row["job_state"] == "dead":
            outcomes["failed"] += 1
        elif row and row["asset_id"]:
            outcomes["stored_pending_embedding"] += 1
        elif item["source_id"] in attempted_ids:
            outcomes["attempted_pending"] += 1
        else:
            outcomes["unattempted"] += 1
    final_ids = {item["source_id"] for item in output}
    before_ready = set(baseline.get("ready_ids", []))
    before_stored = set(baseline.get("stored_ids", []))
    before_embedded = set(baseline.get("embedded_ids", []))
    outcomes["skipped_reused_ready"] = len(final_ids & before_ready)
    outcomes["newly_stored"] = len(final_ids - before_stored)
    outcomes["newly_embedded"] = len(final_ids - before_embedded)
    outcomes["reused_embedding"] = len(final_ids & before_embedded)
    outcomes["attempted"] = len(attempted_ids)
    outcomes["stored_candidates"] = sum(bool(row["asset_id"]) for row in rows.values())
    outcomes["embedded_candidates"] = sum(bool(row["embedded_at"]) for row in rows.values())
    outcomes["retried_ingest_jobs"] = sum((row["attempt_count"] or 0) > 1
                                          for row in rows.values())
    outcomes["missing_active_embedding"] = sum(
        bool(row["asset_id"]) and row["asset_status"] in ("STORED", "READY")
        and not row["embedded_at"] for row in rows.values())
    settings = get_settings()
    client = create_s3_client(settings)
    object_bytes = {"originals": 0, "thumbnails": 0}
    thumbnail_metrics = {}
    try:
        for prefix, field in (("originals", "original_key"), ("thumbnails", "thumbnail_key")):
            expected = {item[field]: item["sha256"] for item in output}
            for key in sorted(expected):
                head = client.head_object(Bucket=settings.object_store_bucket, Key=key)
                if head.get("Metadata", {}).get("sha256") != expected[key]:
                    raise RuntimeError(f"Stored checksum metadata mismatch: {key}")
                object_bytes[prefix] += head["ContentLength"]
        for item in output:
            body = client.get_object(Bucket=settings.object_store_bucket,
                                     Key=item["thumbnail_key"])["Body"].read()
            source_row = rows[item["source_id"]]
            thumbnail_metrics[item["source_id"]] = _visual_metrics(
                body, source_row["width"], source_row["height"])
    finally:
        client.close()
    for item in output:
        item.update(thumbnail_metrics[item["source_id"]])
    _write_jsonl(ROOT / "catalog.jsonl", output)
    compositions = Counter(item["composition"] for item in output)
    hues = Counter(item["dominant_hue"] or "low_saturation" for item in output)
    sampling_counts = Counter(item["sampling_category"] for item in output)
    sampling_disagreement = sum(item["category"] != item["sampling_category"] for item in output)
    rejected_statuses = Counter(row["eligibility_status"] for row in rows.values()
                                if row["eligibility_status"] in
                                ("NON_CC0_CACHED", "UNKNOWN_CACHED"))
    failure_reasons = Counter(row["last_error"] or "unknown" for row in rows.values()
                              if row["job_state"] == "dead")
    low_saturation = sum(item["mean_saturation"] < 0.15 for item in output)
    uncertain = sum(item["visual_score_margin"] < 0.015 for item in output)
    result = {"target": target, "catalog_size": len(output),
              "model_key": MODEL_KEY, "model_version": MODEL_VERSION,
              "catalog_sha256": hashlib.sha256((ROOT / "catalog.jsonl").read_bytes()).hexdigest(),
              "candidates_sha256": hashlib.sha256(
                  (ROOT / "candidates.jsonl").read_bytes()).hexdigest(),
              "category_counts": dict(sorted(counts.items())),
              "sampling_category_counts": dict(sorted(sampling_counts.items())),
              "sampling_visual_disagreement_count": sampling_disagreement,
              "category_method": "OpenCLIP prompt similarity; visually sampled, approximate",
              "composition_counts": dict(sorted(compositions.items())),
              "dominant_hue_counts": dict(sorted(hues.items())),
              "low_saturation_count": low_saturation,
              "uncertain_visual_category_count": uncertain,
              "outcomes": dict(sorted(outcomes.items())),
              "rejected_by_status": dict(sorted(rejected_statuses.items())),
              "failed_by_reason": dict(sorted(failure_reasons.items())),
              "object_bytes": {**object_bytes, "total": sum(object_bytes.values())},
              "verified_at": datetime.now(UTC).isoformat(),
              "baseline_at": baseline.get("recorded_at")}
    (ROOT / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if len(output) != target:
        raise SystemExit(f"Only {len(output)} of {target} verified assets; keep sampling")
    if target == 1000 and outcomes["missing_active_embedding"]:
        raise SystemExit("Stored/READY candidates lack active-model embeddings; "
                         "inspect embed jobs and run the existing reembed command")


def contact_sheet(limit_per_category):
    catalog = _read_jsonl(ROOT / "catalog.jsonl")
    settings = get_settings()
    client = create_s3_client(settings)
    samples = []
    chosen = []
    for category in CATEGORIES:
        group = (item for item in catalog if item["category"] == category)
        ordered = sorted(group, key=lambda item: hashlib.sha256(
            f"review-v1:{item['source_id']}".encode()).digest())
        chosen.extend(ordered[:limit_per_category])
    try:
        for item in chosen:
            body = client.get_object(Bucket=settings.object_store_bucket,
                                     Key=item["thumbnail_key"])["Body"].read()
            with Image.open(io.BytesIO(body)) as image:
                samples.append((item, image.convert("RGB").copy()))
    finally:
        client.close()
    cols, cell_w, cell_h = 8, 150, 175
    sheet = Image.new("RGB", (cols * cell_w, ((len(samples) + cols - 1) // cols) * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (item, image) in enumerate(samples):
        x, y = (index % cols) * cell_w, (index // cols) * cell_h
        image.thumbnail((cell_w - 10, cell_h - 36))
        sheet.paste(image, (x + (cell_w - image.width) // 2, y))
        draw.text((x + 4, y + cell_h - 32), item["category"][:16], fill="black")
        draw.text((x + 4, y + cell_h - 16), item["source_id"], fill="black")
    path = ROOT / "contact-sheet.jpg"
    sheet.save(path, quality=85)
    print(path)


def preview_labels():
    sample = _read_jsonl(ROOT / "pilot-catalog.jsonl")
    labels = _visual_labels([item["source_id"] for item in sample])
    counts = Counter(category for category, _ in labels.values())
    uncertain = sum(margin < 0.015 for _, margin in labels.values())
    print(json.dumps({"pilot_visual_categories": dict(sorted(counts.items())),
                      "uncertain": uncertain}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select_parser = commands.add_parser("select")
    select_parser.add_argument("--per-category", type=int, required=True)
    select_parser.add_argument("--phase", choices=("pilot", "expansion"), required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--target", type=int, required=True)
    run_parser.add_argument("--batch-size", type=int, default=20)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--target", type=int, required=True)
    sheet_parser = commands.add_parser("contact-sheet")
    sheet_parser.add_argument("--limit-per-category", type=int, default=8)
    commands.add_parser("preview-labels")
    args = parser.parse_args()
    if args.command == "select":
        select(args.per_category, args.phase)
    elif args.command == "run":
        run(args.target, args.batch_size)
    elif args.command == "report":
        report(args.target)
    elif args.command == "contact-sheet":
        contact_sheet(args.limit_per_category)
    else:
        preview_labels()


if __name__ == "__main__":
    main()
