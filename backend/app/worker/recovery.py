"""Opt-in command loop for durable ingest retries.

The database owns retry deadlines. This loop can stop at any point and a later
invocation continues from the same job rows; it keeps no progress in memory.
"""

import time
from collections.abc import Callable

from app.ingestion.models import IngestOutcome
from app.worker.ingest import IngestWorker
from app.worker.jobs import JobStore


def follow_ingest(
    worker: IngestWorker,
    jobs: JobStore,
    *,
    max_jobs: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[IngestOutcome]:
    """Drain due work, then wait for queued retry deadlines until none remain."""
    outcomes: list[IngestOutcome] = []
    while True:
        batch = worker.drain(max_jobs)
        outcomes.extend(batch)
        delay = jobs.next_ingest_run_delay()
        if delay is None:
            return outcomes
        # Another worker can hold the due row. Avoid a busy loop in that case.
        sleep(min(delay, 30.0) if delay > 0 else 0.5 if not batch else 0)
