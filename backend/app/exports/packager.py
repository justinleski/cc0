"""Build the export archive. Pure: bytes in, a finished local file out.

The archive is deterministic for a given snapshot: entries are written in
position order with a fixed timestamp, so a retried job that sees the same
catalog produces the same bytes and the same digest. Nothing is uploaded here.
"""

import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import IO, Any

MANIFEST_VERSION = 1
CSV_COLUMNS = (
    "asset_id",
    "filename",
    "title",
    "creator",
    "source",
    "source_landing_url",
    "original_uri",
    "rights_type",
    "license_evidence_url",
    "fetched_at",
    "verified_at",
    "exported_at",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_EXTENSION = re.compile(r"^[a-z0-9]{1,5}$")


@dataclass(frozen=True)
class Packaged:
    file: IO[bytes] | None
    sha256: str
    size: int
    included: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def _cell(value: Any) -> str:
    """Text for a spreadsheet cell. A leading formula character is neutralized."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def filename_for(position: int, asset_id: str, original_key: str) -> str:
    extension = PurePosixPath(original_key).suffix.lstrip(".").lower()
    if not _EXTENSION.match(extension):
        extension = "bin"
    return f"images/{position + 1:03d}-{asset_id}.{extension}"


def build_archive(
    *,
    export_id: str,
    board_id: str | None,
    board_name: str,
    exported_at: datetime,
    items: list[dict[str, Any]],
    read_original: Callable[[str], bytes | None],
    checkpoint: Callable[[], None],
) -> Packaged:
    """Write the zip to a temporary file. Ineligible or unreadable assets are excluded.

    When nothing can be included the result has no file; the caller fails the export.

    `items` are catalog rows (position order) with an `eligible` flag. An asset is
    excluded, with a reason recorded in the manifest, when it is no longer
    eligible, its original is missing, or the stored bytes do not match the
    recorded digest. Object-store outages propagate so the job retries.
    """
    stamp = (max(exported_at.year, 1980), exported_at.month, exported_at.day,
             exported_at.hour, exported_at.minute, exported_at.second)
    exported = _iso(exported_at)
    entries: list[dict[str, Any]] = []
    excluded: list[tuple[str, str]] = []
    file = tempfile.TemporaryFile()  # noqa: SIM115 - owned by the caller via Packaged
    try:
        with zipfile.ZipFile(file, "w", allowZip64=True) as archive:
            for item in items:
                checkpoint()
                asset_id = str(item["asset_id"])
                if not item["eligible"]:
                    excluded.append((asset_id, "rights_changed"))
                    continue
                body = read_original(item["original_key"])
                if body is None:
                    excluded.append((asset_id, "original_missing"))
                    continue
                if hashlib.sha256(body).hexdigest() != item["sha256"]:
                    excluded.append((asset_id, "original_corrupt"))
                    continue
                name = filename_for(item["position"], asset_id, item["original_key"])
                info = zipfile.ZipInfo(name, stamp)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o644 << 16
                archive.writestr(info, body)
                entries.append({**item, "filename": name})

            if entries:
                _write_text(archive, "attribution.csv", stamp, _attribution(entries, exported))
                manifest = _manifest(export_id, board_id, board_name, exported, entries, excluded)
                _write_text(archive, "manifest.json", stamp, manifest)
        if not entries:
            file.close()
            return Packaged(None, "", 0, (), tuple(excluded))
        size = file.tell()
        file.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
        file.seek(0)
    except BaseException:
        file.close()
        raise
    return Packaged(
        file, digest.hexdigest(), size,
        tuple(str(entry["asset_id"]) for entry in entries), tuple(excluded),
    )


def _write_text(archive: zipfile.ZipFile, name: str, stamp: tuple, text: str) -> None:
    info = zipfile.ZipInfo(name, stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, text.encode("utf-8"))


def _attribution(entries: list[dict[str, Any]], exported_at: str | None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                _cell(value)
                for value in (
                    entry["asset_id"], entry["filename"], entry["title"], entry["creator_name"],
                    entry["provider_name"], entry["landing_url"], entry["original_url"],
                    entry["rights_type"], entry["evidence_url"], _iso(entry["last_fetched_at"]),
                    _iso(entry["verified_at"]), exported_at,
                )
            ]
        )
    return buffer.getvalue()


def _manifest(
    export_id: str,
    board_id: str | None,
    board_name: str,
    exported_at: str | None,
    entries: list[dict[str, Any]],
    excluded: list[tuple[str, str]],
) -> str:
    return json.dumps(
        {
            "manifest_version": MANIFEST_VERSION,
            "export_id": export_id,
            "board": {"id": board_id, "name": board_name},
            "exported_at": exported_at,
            "assets": [
                {
                    "asset_id": str(entry["asset_id"]),
                    "filename": entry["filename"],
                    "sha256": entry["sha256"],
                    "mime_type": entry["mime_type"],
                    "width": entry["width"],
                    "height": entry["height"],
                    "title": entry["title"],
                    "creator": entry["creator_name"],
                    "source": {
                        "provider": entry["provider_name"],
                        "external_id": entry["external_id"],
                        "landing_url": entry["landing_url"],
                        "original_uri": entry["original_url"],
                        "fetched_at": _iso(entry["last_fetched_at"]),
                    },
                    "rights": {
                        "type": entry["rights_type"],
                        "verification_status": entry["verification_status"],
                        "verification_method": entry["verification_method"],
                        "evidence_url": entry["evidence_url"],
                        "statement": entry["raw_statement"],
                        "verified_at": _iso(entry["verified_at"]),
                    },
                }
                for entry in entries
            ],
            "excluded": [{"asset_id": asset, "reason": reason} for asset, reason in excluded],
        },
        indent=2,
        ensure_ascii=False,
    )
