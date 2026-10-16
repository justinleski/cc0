"""Operator entry point: `python -m app.worker <command>`.

    ingest   discover (or take explicit ids), queue what is due, run ingest then embed
    drain    run already queued ingest jobs, then embed jobs
    reembed  queue assets missing the active checkpoint and run those jobs
    jobs     list jobs in one state, which is how dead letters are inspected
    redrive  return a dead job to pending and reset its attempts

The loop is single-threaded on purpose: one claimed job means one in-flight
source request per provider.
"""

import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import catalog
from app.embeddings.models import EmbedOutcome
from app.ingestion.models import IngestOutcome
from app.worker.runtime import WorkerRuntime, build_runtime

logger = logging.getLogger("cc0.worker")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.worker")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="discover, queue, and run")
    ingest.add_argument("--limit", type=int, default=None, help="records to discover")
    ingest.add_argument(
        "--object-id",
        action="append",
        default=[],
        dest="object_ids",
        help="ingest an explicit external id instead of discovering",
    )

    drain = commands.add_parser("drain", help="run queued jobs")
    drain.add_argument("--max-jobs", type=int, default=None)

    commands.add_parser("reembed", help="queue and run missing embeddings for the active model")

    listing = commands.add_parser("jobs", help="list jobs by state")
    listing.add_argument("--state", default=catalog.JOB_DEAD, choices=list(catalog.JOB_STATES))
    listing.add_argument("--limit", type=int, default=50)

    redrive = commands.add_parser("redrive", help="retry a dead job")
    group = redrive.add_mutually_exclusive_group(required=True)
    group.add_argument("--dedupe-key")
    group.add_argument("--all-dead", action="store_true")
    return parser


def _report(outcomes: list[IngestOutcome]) -> int:
    for outcome in outcomes:
        logger.info(
            "ingest external_id=%s result=%s detail=%s",
            outcome.external_id,
            outcome.result,
            outcome.detail,
        )
    # A dead letter is an operator signal, so the command fails even though the
    # rest of the batch succeeded.
    return 1 if any(outcome.result == catalog.JOB_DEAD for outcome in outcomes) else 0


def _report_embed(outcomes: list[EmbedOutcome]) -> int:
    for outcome in outcomes:
        logger.info(
            "embed asset_id=%s result=%s detail=%s",
            outcome.asset_id,
            outcome.result,
            outcome.detail,
        )
    return 1 if any(outcome.result == catalog.JOB_DEAD for outcome in outcomes) else 0


def _run_embed(runtime: WorkerRuntime, max_jobs: int | None) -> int:
    """Drain embed jobs. Without a cap, keep going until the queue is idle or one dies."""
    code = 0
    rounds = 1 if max_jobs is not None else 20
    for _ in range(rounds):
        outcomes = runtime.embed_worker.drain(max_jobs)
        code = max(code, _report_embed(outcomes))
        if not outcomes or code or max_jobs is not None:
            break
    return code


def _dispatch(args: argparse.Namespace, runtime: WorkerRuntime) -> int:
    service = runtime.service
    if args.command == "ingest":
        external_ids = args.object_ids or service.discover(args.limit)
        for external_id, reason in service.enqueue(list(external_ids)).items():
            logger.info("skipped external_id=%s reason=%s", external_id, reason)
        ingest_code = _report(runtime.ingest_worker.drain())
        return max(ingest_code, _run_embed(runtime, None))

    if args.command == "drain":
        ingest_code = _report(runtime.ingest_worker.drain(args.max_jobs))
        return max(ingest_code, _run_embed(runtime, args.max_jobs))

    if args.command == "reembed":
        queued = runtime.embed_service.enqueue_missing()
        logger.info(
            "reembed queued=%s model=%s version=%s",
            queued,
            runtime.embeddings.model_key,
            runtime.embeddings.model_version,
        )
        return _run_embed(runtime, None)

    if args.command == "jobs":
        for row in runtime.job_store.list_by_state(args.state, args.limit):
            print(
                f"{row.dedupe_key} state={row.state} attempts={row.attempt_count} "
                f"last_error={row.last_error or '-'}"
            )
        return 0

    keys = (
        [row.dedupe_key for row in runtime.job_store.list_by_state(catalog.JOB_DEAD, limit=1000)]
        if args.all_dead
        else [args.dedupe_key]
    )
    for key in keys:
        if runtime.job_store.redrive(key):
            logger.info("redriven dedupe_key=%s", key)
        else:
            logger.warning("not dead, nothing to redrive dedupe_key=%s", key)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    runtime = build_runtime(get_settings())
    try:
        return _dispatch(args, runtime)
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.exit(main())
