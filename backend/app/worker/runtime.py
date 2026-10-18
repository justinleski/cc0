"""Wiring for the worker process.

The worker owns its own database pool and object-store client. It never borrows
the API's, because a pool cannot cross a process boundary ([ADR-0004]).

`fixture` mode replays recorded provider responses and opens no socket, so the
default local run and CI need neither network access nor an API key. `network`
mode is the opt-in path and is the only one that paces requests.

[ADR-0004]: ../../../docs/decisions/0004-host-processes.md
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from app.core.config import Settings
from app.db.engine import create_worker_engine
from app.embeddings.provider import EmbeddingProvider, build_provider
from app.embeddings.repository import EmbeddingRepository
from app.embeddings.service import EmbedService
from app.exports.package_service import PackageService
from app.exports.worker_repository import ExportWorkerRepository
from app.ingestion.fetch import HttpImageFetcher, HttpJsonSource, Pacer
from app.ingestion.fixtures import FixtureSource
from app.ingestion.met import MetAdapter
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestService
from app.integrations.object_store import ObjectStore, create_s3_client
from app.worker.embed import EmbedWorker
from app.worker.export import ExportWorker
from app.worker.ingest import IngestWorker
from app.worker.jobs import JobStore


@dataclass
class WorkerRuntime:
    settings: Settings
    engine: Engine
    service: IngestService
    embed_service: EmbedService
    ingest_worker: IngestWorker
    embed_worker: EmbedWorker
    export_worker: ExportWorker
    export_service: PackageService
    embeddings: EmbeddingProvider
    job_store: JobStore
    # The recorded-fixture source in fixture mode, which also carries the
    # request log. Tests assert against it; the CLI does not use it.
    source: Any
    _closers: list[Any]

    def close(self) -> None:
        for closer in self._closers:
            closer()
        self.engine.dispose()


def build_runtime(settings: Settings) -> WorkerRuntime:
    engine = create_worker_engine(settings)
    s3 = create_s3_client(settings, timeout_seconds=settings.ingest_request_timeout_seconds)
    closers: list[Any] = [s3.close]

    if settings.ingest_source_mode == "fixture":
        source = FixtureSource(
            root=settings.fixture_dir,
            metadata_hosts=settings.metadata_host_allowlist,
            image_hosts=settings.image_host_allowlist,
        )
        json_source: Any = source
        image_fetcher: Any = source
    else:
        # One pacer for both clients: the budget belongs to the provider, not
        # to a single endpoint.
        pacer = Pacer(settings.ingest_rate_limit_rps)
        json_source = HttpJsonSource(
            settings.metadata_host_allowlist,
            timeout_seconds=settings.ingest_request_timeout_seconds,
            pacer=pacer,
        )
        image_fetcher = HttpImageFetcher(
            settings.image_host_allowlist,
            max_bytes=settings.ingest_max_image_bytes,
            timeout_seconds=settings.ingest_request_timeout_seconds,
            max_redirects=settings.ingest_max_redirects,
            pacer=pacer,
        )
        source = json_source
        closers.extend([json_source.close, image_fetcher.close])

    job_store = JobStore(engine)
    embedding_repository = EmbeddingRepository(engine)
    object_store = ObjectStore(s3, settings.object_store_bucket)
    embeddings = build_provider(settings)
    service = IngestService(
        settings=settings,
        adapter=MetAdapter(json_source),
        image_fetcher=image_fetcher,
        object_store=object_store,
        repository=IngestionRepository(engine),
        job_store=job_store,
        embedding_model_key=embeddings.model_key,
        embedding_model_version=embeddings.model_version,
    )
    embed_service = EmbedService(
        settings=settings,
        embeddings=embeddings,
        object_store=object_store,
        repository=embedding_repository,
        job_store=job_store,
    )
    export_service = PackageService(
        settings=settings,
        repository=ExportWorkerRepository(engine),
        object_store=object_store,
    )
    return WorkerRuntime(
        settings=settings,
        engine=engine,
        service=service,
        embed_service=embed_service,
        ingest_worker=IngestWorker(settings=settings, job_store=job_store, service=service),
        embed_worker=EmbedWorker(settings=settings, job_store=job_store, service=embed_service),
        export_service=export_service,
        export_worker=ExportWorker(
            settings=settings, job_store=job_store, service=export_service
        ),
        embeddings=embeddings,
        job_store=job_store,
        source=source,
        _closers=closers,
    )
