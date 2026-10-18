"""Export request and status rules; no HTTP and no SQL."""

from dataclasses import replace
from uuid import UUID

from app.core.config import Settings
from app.db import catalog
from app.exports.models import Export
from app.exports.repository import ExportRepository
from app.integrations.object_store import ObjectStore

RIGHTS_CHANGED = "rights_changed"


class ExportService:
    def __init__(self, repository: ExportRepository, urls: ObjectStore, settings: Settings):
        self._repository = repository
        self._urls = urls
        self._settings = settings

    def create(self, owner_id: UUID, board_id: UUID) -> Export:
        return self._repository.create(
            owner_id,
            board_id,
            max_assets=self._settings.export_max_assets,
            max_active=self._settings.export_max_active_per_owner,
            max_attempts=self._settings.ingest_max_attempts,
        )

    def read(self, owner_id: UUID, export_id: UUID) -> Export:
        """Status, plus a download URL only while the archive is still safe to hand out.

        The rights recheck happens on every read, not once at packaging. A READY
        archive whose asset was suppressed afterwards is reported as failed and
        never gets a URL; the owner requests a new export, which excludes it.
        """
        export, key, still_eligible = self._repository.get(owner_id, export_id)
        if export.status != catalog.EXPORT_READY:
            return export
        if not still_eligible or key is None:
            return replace(export, status=catalog.EXPORT_FAILED, failure_reason=RIGHTS_CHANGED)
        ttl = self._settings.export_download_url_seconds
        url = self._urls.presigned_get(key, ttl, f"cc0-board-export-{export.id}.zip")
        return replace(export, download_url=url, download_expires_in=ttl)

    def list_for_board(self, owner_id: UUID, board_id: UUID, limit: int) -> list[Export]:
        return self._repository.list_for_board(owner_id, board_id, limit)
