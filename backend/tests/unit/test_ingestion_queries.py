import json
from uuid import uuid4

from app.db import catalog
from app.ingestion import queries


def test_rights_update_binds_metadata_and_preserves_database_clock_deadline():
    value = "'); DROP TABLE source_records; --"
    record_id = uuid4()
    statement, params = queries.rights_update_query(
        record_id, value, value, {"title": value}, value, value, 7
    )
    assert value not in statement
    assert params["id"] == record_id
    assert params["landing_url"] == params["original_url"] == value
    assert json.loads(params["raw_metadata"]) == {"title": value}
    assert params["classification"] == params["eligibility"] == value
    assert params["recheck_days"] == 7
    assert "now() + make_interval" in statement
    assert "IS NULL THEN NULL" in statement
    _, eligible = queries.rights_update_query(record_id, None, None, {}, "CC0", "ok", None)
    assert eligible["recheck_days"] is None


def test_creator_query_binds_names_and_preserves_external_ids_merge():
    value = "O'Brien'; --"
    statement, params = queries.creator_query(uuid4(), value, None, {"authority": value})
    assert value not in statement
    assert params["external_key"] == params["name"] == value
    assert json.loads(params["external_ids"]) == {"authority": value}
    assert "creators.external_ids || EXCLUDED.external_ids" in statement


def test_embed_enqueue_preserves_payload_identity_and_conflict_behavior():
    asset_id = uuid4()
    statement, params = queries.embed_job_query(
        asset_id, "fake'; --", "deterministic-v1", "originals/a", "embed:key", 4
    )
    assert "fake'; --" not in statement
    assert params["kind"] == catalog.JOB_KIND_EMBED
    assert params["state"] == catalog.JOB_PENDING
    assert params["dedupe_key"] == "embed:key"
    assert params["max_attempts"] == 4
    assert json.loads(params["payload"]) == {
        "asset_id": str(asset_id),
        "model_key": "fake'; --",
        "model_version": "deterministic-v1",
        "original_key": "originals/a",
    }
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in statement


def test_latest_evidence_uses_deterministic_order_and_source_reads_use_database_clock():
    assert "ORDER BY checked_at DESC, id DESC" in queries.LATEST_EVIDENCE
    assert "LIMIT 1" in queries.LATEST_EVIDENCE
    for statement in (queries.GET_SOURCE_RECORD, queries.UPSERT_SOURCE_RECORD):
        assert "next_recheck_at <= now()" in statement
        assert ":provider_id" in statement and ":external_id" in statement
