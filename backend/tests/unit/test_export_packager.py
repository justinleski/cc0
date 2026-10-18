"""Archive building: determinism, exclusion rules, and spreadsheet-safe attribution."""

import csv
import hashlib
import io
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

from app.exports.packager import build_archive, filename_for

NOW = datetime(2026, 10, 18, 12, 30, tzinfo=UTC)


def _item(position: int, body: bytes, **overrides):
    asset_id = uuid4()
    item = {
        "asset_id": asset_id,
        "position": position,
        "title": "Harbor",
        "sha256": hashlib.sha256(body).hexdigest(),
        "mime_type": "image/jpeg",
        "width": 10,
        "height": 10,
        "original_key": f"originals/aa/bb/{position}.jpg",
        "creator_name": "Anon",
        "provider_name": "met",
        "external_id": str(position),
        "landing_url": "https://example.test/1",
        "original_url": "https://images.example.test/1.jpg",
        "last_fetched_at": NOW,
        "rights_type": "CC0",
        "verification_status": "VERIFIED",
        "verification_method": "provider_policy",
        "evidence_url": "https://example.test/policy",
        "raw_statement": "Open Access",
        "verified_at": NOW,
        "eligible": True,
    }
    item.update(overrides)
    return item, body


def _build(pairs, store=None):
    bodies = {item["original_key"]: body for item, body in pairs}
    bodies.update(store or {})
    return build_archive(
        export_id="e1",
        board_id="b1",
        board_name="Board",
        exported_at=NOW,
        items=[item for item, _ in pairs],
        read_original=bodies.get,
        checkpoint=lambda: None,
    )


def test_archive_is_deterministic_and_complete():
    pairs = [_item(0, b"one"), _item(1, b"two")]
    first, second = _build(pairs), _build(pairs)
    assert first.sha256 == second.sha256 and first.size == second.size
    with zipfile.ZipFile(first.file) as archive:
        assert archive.testzip() is None
        assert archive.namelist()[-2:] == ["attribution.csv", "manifest.json"]
        assert len(archive.namelist()) == 4


def test_ineligible_missing_and_corrupt_originals_are_excluded_with_reasons():
    keep, _ = _item(0, b"ok")
    gone = _item(1, b"gone")
    bad = _item(2, b"real")
    bad[0]["sha256"] = hashlib.sha256(b"different").hexdigest()
    withdrawn = _item(3, b"x", eligible=False)
    result = build_archive(
        export_id="e1", board_id=None, board_name="B", exported_at=NOW,
        items=[keep, gone[0], bad[0], withdrawn[0]],
        read_original={keep["original_key"]: b"ok", bad[0]["original_key"]: b"real"}.get,
        checkpoint=lambda: None,
    )
    assert result.included == (str(keep["asset_id"]),)
    assert dict(result.excluded) == {
        str(gone[0]["asset_id"]): "original_missing",
        str(bad[0]["asset_id"]): "original_corrupt",
        str(withdrawn[0]["asset_id"]): "rights_changed",
    }


def test_nothing_includable_yields_no_file():
    result = _build([_item(0, b"x", eligible=False)])
    assert result.file is None and result.included == ()


def test_attribution_neutralizes_spreadsheet_formulas():
    pair = _item(0, b"x", title="=HYPERLINK(\"http://evil\")", creator_name="@cmd")
    with zipfile.ZipFile(_build([pair]).file) as archive:
        row = next(csv.DictReader(io.StringIO(archive.read("attribution.csv").decode())))
    assert row["title"].startswith("'=") and row["creator"] == "'@cmd"


def test_filenames_are_derived_not_user_supplied():
    asset = "a" * 36
    assert filename_for(0, asset, "originals/x/y.JPG") == f"images/001-{asset}.jpg"
    assert filename_for(9, asset, "originals/x/y.../../evil") == f"images/010-{asset}.bin"
