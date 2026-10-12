"""Keeping a claim alive while one job runs, and knowing when it is gone.

A lease is a promise that this worker is still working. The pipeline makes
calls nothing in the process can bound on its own: DNS resolution has no
application deadline, an HTTP timeout is per read rather than per transfer, and
an object PUT waits on another service. Any of those can outlive a fixed lease,
after which another worker may reclaim the job and finish it.

So the lease is renewed from a background thread on its own pool connection,
and the pipeline asks `check()` before it does anything expensive or commits.
Renewal is not unconditional: it stops at a total deadline for the job, which
is what keeps a stalled transfer from holding the claim indefinitely.
"""

import logging
import threading

from app.ingestion.errors import TransientFailure
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore

logger = logging.getLogger("cc0.worker")

# Renew well inside the lease, so one missed tick is not a lost claim.
_RENEWALS_PER_LEASE = 3


class LeaseKeeper:
    """Renews one claim until the job finishes, is lost, or runs out of time."""

    def __init__(
        self,
        job_store: JobStore,
        job: ClaimedJob,
        *,
        lease_seconds: int,
        deadline_seconds: float,
    ) -> None:
        self._jobs = job_store
        self._job = job
        self._lease_seconds = lease_seconds
        self._deadline_seconds = deadline_seconds
        self._interval = max(lease_seconds / _RENEWALS_PER_LEASE, 1.0)
        self._stopped = threading.Event()
        self._lost = threading.Event()
        self._expired = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseKeeper":
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-{self._job.dedupe_key}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval)

    def check(self) -> None:
        """Raise if this work can no longer be committed. Cheap, no I/O.

        Reading the flags the renewal thread sets, rather than querying, keeps
        this safe to call between chunks of a download.
        """
        if self._lost.is_set():
            raise ClaimLost(self._job.id)
        if self._expired.is_set():
            raise TransientFailure("job_deadline_exceeded", str(self._deadline_seconds))

    def _run(self) -> None:
        waited = 0.0
        while not self._stopped.wait(self._interval):
            waited += self._interval
            if waited >= self._deadline_seconds:
                # Let the lease lapse on purpose. Another worker can then take
                # a job this one has been stuck on for too long.
                self._expired.set()
                logger.warning(
                    "job deadline exceeded dedupe_key=%s seconds=%.0f",
                    self._job.dedupe_key,
                    waited,
                )
                return
            if not self._jobs.renew_lease(self._job, self._lease_seconds):
                self._lost.set()
                logger.warning("lease lost dedupe_key=%s", self._job.dedupe_key)
                return
