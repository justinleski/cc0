"""Execution adapters own claims and settlement around feature workflows."""

import inspect
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db import catalog
from app.embeddings.models import EmbedOutcome
from app.embeddings.service import EmbedService
from app.ingestion.errors import PermanentFailure, TransientFailure
from app.ingestion.models import IngestOutcome
from app.ingestion.service import IngestService
from app.integrations.object_store import ObjectStoreUnavailable
from app.worker import embed, ingest
from app.worker.jobs import ClaimedJob, ClaimLost, JobStore


@pytest.fixture(params=[ingest, embed])
def execution(request, monkeypatch):
    module = request.param
    settings = Settings()
    jobs = Mock(spec=JobStore)
    service = Mock(spec=IngestService if module is ingest else EmbedService)
    service.model_key = "fake"
    service.model_version = "v1"
    job = ClaimedJob(
        uuid4(),
        "ingest" if module is ingest else "embed",
        "key",
        1,
        4,
        {"external_id": "100001", "asset_id": str(uuid4())},
        uuid4(),
    )
    lease = Mock()
    lease.__enter__ = Mock(return_value=lease)
    lease.__exit__ = Mock(return_value=False)
    keeper = Mock(return_value=lease)
    monkeypatch.setattr(module, "LeaseKeeper", keeper)
    cls = ingest.IngestWorker if module is ingest else embed.EmbedWorker
    worker = cls(settings=settings, job_store=jobs, service=service)
    return module, worker, jobs, service, job, lease, keeper


@pytest.mark.parametrize("cls", [IngestService, EmbedService])
def test_feature_services_have_no_execution_lifecycle(cls):
    source = inspect.getsource(cls)
    for name in ("drain", "run_claimed", "_schedule_retry"):
        assert not hasattr(cls, name)
    for operation in (
        ".claim(",
        ".reap_exhausted(",
        ".renew_lease(",
        "LeaseKeeper",
        ".retry_later(",
        ".mark_dead(",
        ".succeed(",
    ):
        assert operation not in source


def test_drain_claims_and_invokes_workflow_inside_lease(execution):
    module, worker, jobs, service, job, lease, keeper = execution
    events = []
    jobs.reap_exhausted.side_effect = lambda **_: events.append("reap")
    jobs.claim.side_effect = [job, None]
    lease.__enter__.side_effect = lambda: events.append("enter") or lease
    lease.__exit__.side_effect = lambda *_: events.append("exit") or False
    outcome = (
        IngestOutcome("100001", "stored", job_settled=True)
        if module is ingest
        else EmbedOutcome(job.payload["asset_id"], "embedded", job_settled=True)
    )
    service.execute.side_effect = lambda *args: events.append("execute") or outcome
    assert worker.drain(2) == [outcome]
    assert events == ["reap", "enter", "execute", "exit"]
    if module is ingest:
        service.execute.assert_called_once_with(job, "100001", lease.check)
        jobs.claim.assert_called_with(lease_seconds=worker._settings.ingest_lease_seconds)
    else:
        service.prepare.assert_called_once_with()
        service.execute.assert_called_once_with(job)
        jobs.claim.assert_called_with(
            lease_seconds=worker._settings.ingest_lease_seconds,
            kind=catalog.JOB_KIND_EMBED,
            model_key="fake",
            model_version="v1",
        )
    keeper.assert_called_once()
    jobs.succeed.assert_not_called()  # Repository already settled atomically.


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("explicit_delay", [None, 17])
def test_retry_policy_and_attempt_exhaustion(execution, monkeypatch, exhausted, explicit_delay):
    module, worker, jobs, service, job, _, _ = execution
    job = replace(job, attempt_count=4 if exhausted else 1)
    service.execute.side_effect = TransientFailure("temporary", "", retry_after=explicit_delay)
    backoff = Mock(return_value=23)
    monkeypatch.setattr(module.job_module, "backoff_seconds", backoff)
    outcome = worker.run_claimed(job)
    assert outcome.result == (catalog.JOB_DEAD if exhausted else catalog.JOB_RETRY_WAIT)
    if exhausted:
        jobs.mark_dead.assert_called_once_with(job, "temporary")
        jobs.retry_later.assert_not_called()
        backoff.assert_not_called()
    else:
        jobs.retry_later.assert_called_once_with(job, "temporary", explicit_delay or 23)
        if explicit_delay is None:
            backoff.assert_called_once_with(
                job.attempt_count,
                base_seconds=worker._settings.ingest_backoff_base_seconds,
                max_seconds=worker._settings.ingest_backoff_max_seconds,
            )
        else:
            backoff.assert_not_called()


@pytest.mark.parametrize(
    "failure, result, reason",
    [
        (PermanentFailure("invalid", ""), "dead", "invalid"),
        (ObjectStoreUnavailable("unavailable"), "retry_wait", "object_store"),
        (ClaimLost(uuid4()), "claim_lost", None),
    ],
)
def test_failure_settlement_and_stale_settlement(execution, failure, result, reason):
    _, worker, jobs, service, job, _, _ = execution
    service.execute.side_effect = failure
    assert worker.run_claimed(job).result == result
    if reason is None:
        jobs.mark_dead.assert_not_called()
        jobs.retry_later.assert_not_called()
    else:
        settlement = jobs.mark_dead if result == "dead" else jobs.retry_later
        assert settlement.call_args.args[:2] == (job, reason)
        settlement.return_value = False
        assert worker.run_claimed(job).result == "claim_lost"


def test_unexpected_exception_propagates_after_lease_cleanup(execution):
    _, worker, jobs, service, job, lease, _ = execution
    service.execute.side_effect = RuntimeError("crash")
    with pytest.raises(RuntimeError, match="crash"):
        worker.run_claimed(job)
    lease.__exit__.assert_called_once()
    jobs.succeed.assert_not_called()
    jobs.mark_dead.assert_not_called()
    jobs.retry_later.assert_not_called()


def test_ingestion_cancellation_check_is_passed_through(execution):
    module, worker, jobs, service, job, lease, _ = execution
    if module is embed:
        return  # Embedding uses repository fencing, as before the split.
    lease.check.side_effect = ClaimLost(job.id)
    service.execute.side_effect = lambda job, external_id, check_active: check_active()
    assert worker.run_claimed(job).result == "claim_lost"
    jobs.succeed.assert_not_called()
