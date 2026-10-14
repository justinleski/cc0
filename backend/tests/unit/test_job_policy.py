"""Retry arithmetic and job identity, without a database."""

import random

import pytest

from app.db import catalog
from app.worker.jobs import RESETTABLE_STATES, JobStore, backoff_seconds, dedupe_key


def test_dedupe_key_is_stable_per_provider_and_external_id():
    assert dedupe_key("met", "100001") == "ingest:met:100001"
    # A repeat run or a duplicate delivery recomputes the same key, which is
    # what makes the unique constraint prevent a second download.
    assert dedupe_key("met", "100001") == dedupe_key("met", "100001")
    assert dedupe_key("met", "100001") != dedupe_key("met", "100002")


def test_backoff_grows_and_is_capped():
    windows = [
        backoff_seconds(attempt, base_seconds=2.0, max_seconds=30.0, rng=random.Random(7))
        for attempt in (1, 2, 3, 10)
    ]
    assert windows[0] < windows[1] < windows[2]
    # Capped, and a retry always waits at least half the window.
    assert 15.0 <= windows[3] <= 30.0


def test_jitter_keeps_a_failed_batch_from_retrying_in_lockstep():
    delays = {
        backoff_seconds(2, base_seconds=2.0, max_seconds=30.0, rng=random.Random(seed))
        for seed in range(25)
    }
    assert len(delays) > 1
    assert all(2.0 <= delay <= 4.0 for delay in delays)


def test_only_a_finished_job_is_resettable_by_a_repeat_ingest():
    # A failure an operator has not seen must survive someone re-running
    # ingestion, and an in-flight job belongs to the worker holding its lease.
    assert catalog.JOB_DEAD not in RESETTABLE_STATES
    assert catalog.JOB_LEASED not in RESETTABLE_STATES
    # A job that is waiting out a backoff keeps the attempts it has spent, so a
    # repeat ingestion cannot hand a failing record an unlimited retry budget.
    assert catalog.JOB_RETRY_WAIT not in RESETTABLE_STATES
    assert catalog.JOB_PENDING not in RESETTABLE_STATES
    assert set(RESETTABLE_STATES) == {catalog.JOB_SUCCEEDED}


def test_an_embed_claim_names_the_loaded_checkpoint():
    store = JobStore(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="loaded checkpoint"):
        store.claim(lease_seconds=30, kind=catalog.JOB_KIND_EMBED)
