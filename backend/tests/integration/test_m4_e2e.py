"""M4 end to end: board, pins, both recommendation modes, export, worker, download.

One journey through the public API with the real worker code and object store.
It needs the recommendation engine; until that feature is merged the module is
skipped rather than reported as passing.
"""

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app  # noqa: E402

# Checked on disk: the editable install can resolve modules from another checkout.
if not (Path(app.__file__).parent / "recommendations" / "router.py").exists():
    pytest.skip("recommendation engine not merged yet", allow_module_level=True)

from app.main import create_app  # noqa: E402
from tests.integration.test_boards import _settings, empty_private_tables  # noqa: E402
from tests.integration.test_exports import Harness  # noqa: E402
from tests.integration.test_search import ReadyCatalog, _publish, ready_catalog  # noqa: E402

_ = (ready_catalog, empty_private_tables)
CC0_FIXTURES = [f"1000{index:02d}" for index in range(1, 11)]


def _recommend(client: TestClient, headers, board_id: str, mode: str) -> dict:
    response = client.get(
        f"/api/v1/boards/{board_id}/recommendations",
        headers=headers,
        params={"mode": mode, "limit": 20},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_board_to_download_journey(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, CC0_FIXTURES)
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        harness = Harness(ready_catalog, client, settings)
        alice, bob = harness.alice, harness.bob
        listing = client.get("/api/v1/assets?limit=50").json()["items"]
        catalog_ids = [item["id"] for item in listing]
        assert len(catalog_ids) == 10

        created_board = client.post("/api/v1/boards", headers=alice, json={"name": "Journey"})
        board_id = created_board.json()["id"]
        # Empty board: discovery fallback, and nothing to export yet.
        empty = _recommend(client, alice, board_id, "cohesive")
        assert empty["fallback"] == "browse" and empty["items"]
        assert harness.request(board_id).status_code == 409

        pins = catalog_ids[:3]
        for asset_id in pins:
            client.post(
                f"/api/v1/boards/{board_id}/items", headers=alice, json={"asset_id": asset_id}
            )

        results = {}
        for mode in ("cohesive", "complement"):
            body = _recommend(client, alice, board_id, mode)
            ids = [item["id"] for item in body["items"]]
            assert body["mode"] == mode and ids, mode
            assert not set(ids) & set(pins), "a saved pin was recommended"
            assert set(ids) <= set(catalog_ids), "recommendation outside the verified catalog"
            results[mode] = ids

        # Pinning a suggestion changes what is recommended next and drops it from the list.
        taken = results["cohesive"][0]
        client.post(f"/api/v1/boards/{board_id}/items", headers=alice, json={"asset_id": taken})
        after = [item["id"] for item in _recommend(client, alice, board_id, "cohesive")["items"]]
        assert taken not in after
        assert after != results["cohesive"]

        # Bob can reach neither recommendations nor exports for Alice's board.
        assert (
            client.get(f"/api/v1/boards/{board_id}/recommendations", headers=bob).status_code == 404
        )
        assert client.post(f"/api/v1/boards/{board_id}/exports", headers=bob).status_code == 404

        created = harness.request(board_id)
        assert created.status_code == 202
        export_id = created.json()["id"]
        assert harness.status(export_id).json()["status"] == "QUEUED"
        assert [o.result for o in harness.worker.drain()] == ["published"]

        status = harness.status(export_id).json()
        assert status["status"] == "READY" and status["included_count"] == 4
        archive = zipfile.ZipFile(io.BytesIO(httpx.get(status["download_url"]).content))
        manifest = json.loads(archive.read("manifest.json"))
        rows = list(csv.DictReader(io.StringIO(archive.read("attribution.csv").decode())))
        expected = pins + [taken]
        assert [entry["asset_id"] for entry in manifest["assets"]] == expected
        assert [row["asset_id"] for row in rows] == expected
        for entry in manifest["assets"]:
            assert hashlib.sha256(archive.read(entry["filename"])).hexdigest() == entry["sha256"]
            assert entry["rights"]["type"] == "CC0" and entry["rights"]["evidence_url"]
        assert harness.status(export_id, bob).status_code == 404
