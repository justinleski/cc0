"""Board recommendations against migrated PostgreSQL and fake-embedded CC0 assets.

Fake vectors carry no visual meaning, so these tests check visibility,
ownership, exclusion, fallback, determinism, and the single-pin baseline, not
whether suggestions look right.
"""

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import catalog
from app.main import create_app
from tests.integration.test_boards import KEY, _headers, _settings, empty_private_tables
from tests.integration.test_search import ReadyCatalog, _publish, _reject, ready_catalog

# Imported fixtures reuse the isolated DB, bucket, and private-table reset.
_ = (ready_catalog, empty_private_tables, KEY)
CC0_FIXTURES = [f"1000{index:02d}" for index in range(1, 11)]


@pytest.fixture
def published(ready_catalog: ReadyCatalog) -> ReadyCatalog:
    _publish(ready_catalog, CC0_FIXTURES)
    return ready_catalog


def _board(client: TestClient, headers: dict[str, str], *asset_ids: str) -> str:
    created = client.post("/api/v1/boards", headers=headers, json={"name": "Moodboard"})
    assert created.status_code == 201, created.text
    board_id = created.json()["id"]
    for asset_id in asset_ids:
        added = client.post(
            f"/api/v1/boards/{board_id}/items", headers=headers, json={"asset_id": asset_id}
        )
        assert added.status_code == 201, added.text
    return board_id


def _recommend(client: TestClient, headers: dict[str, str], board_id: str, **params) -> dict:
    response = client.get(
        f"/api/v1/boards/{board_id}/recommendations", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


def _restore(ready: ReadyCatalog, asset_id: str) -> None:
    with psycopg.connect(ready.database.migration_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO rights_evidence (
                source_record_id, asset_id, rights_type, verification_status,
                verification_method, checked_at
            )
            SELECT source_record_id, id, %s, %s, %s, clock_timestamp()
            FROM assets WHERE id = %s
            """,
            (
                catalog.RIGHTS_CC0,
                catalog.VERIFICATION_VERIFIED,
                catalog.METHOD_OPERATOR_REVIEW,
                UUID(asset_id),
            ),
        )
        connection.execute(
            "UPDATE assets SET status = %s WHERE id = %s", (catalog.ASSET_READY, UUID(asset_id))
        )


def test_recommendations_follow_pins_and_match_the_single_pin_baseline(
    published: ReadyCatalog,
) -> None:
    settings = _settings(published)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        public = [item["id"] for item in client.get("/api/v1/assets").json()["items"]]
        assert len(public) == 10
        first, second = public[0], public[1]

        board_id = _board(client, alice)
        empty = _recommend(client, alice, board_id)
        assert empty["fallback"] == "browse"
        assert empty["profile_pin_count"] == 0
        assert _ids(empty) == public

        client.post(f"/api/v1/boards/{board_id}/items", headers=alice, json={"asset_id": first})
        single = _recommend(client, alice, board_id, limit=12)
        assert single["fallback"] is None
        assert single["strategy"] == "centroid-cosine"
        assert single["profile_pin_count"] == 1
        related = client.get(f"/api/v1/assets/{first}/related", params={"limit": 12}).json()
        assert _ids(single) == [item["id"] for item in related["items"]]
        assert all(item["closest_pin_id"] == first for item in single["items"])
        assert _ids(_recommend(client, alice, board_id, limit=12)) == _ids(single)

        client.post(f"/api/v1/boards/{board_id}/items", headers=alice, json={"asset_id": second})
        pair = _recommend(client, alice, board_id)
        assert pair["board_version"] != single["board_version"]
        assert pair["profile_pin_count"] == 2
        assert {first, second}.isdisjoint(_ids(pair))
        assert len(_ids(pair)) == len(set(_ids(pair))) == 8
        assert _ids(pair) != [item for item in _ids(single) if item != second]

        client.delete(f"/api/v1/boards/{board_id}/items/{first}", headers=alice)
        after = _recommend(client, alice, board_id)
        assert first in _ids(after)
        assert second not in _ids(after)

        complement = _recommend(client, alice, board_id, mode="complement")
        assert complement["strategy"] == "complement-baseline-v0"
        assert second not in _ids(complement)


def test_recommendations_are_owner_scoped(published: ReadyCatalog) -> None:
    settings = _settings(published)
    with TestClient(create_app(settings)) as client:
        alice, bob = _headers(settings, "alice"), _headers(settings, "bob")
        public = [item["id"] for item in client.get("/api/v1/assets").json()["items"]]
        board_id = _board(client, alice, public[0])
        path = f"/api/v1/boards/{board_id}/recommendations"
        assert client.get(path).status_code == 401
        foreign = client.get(path, headers=bob)
        guessed = client.get(f"/api/v1/boards/{uuid4()}/recommendations", headers=bob)
        assert foreign.status_code == guessed.status_code == 404
        assert foreign.json() == guessed.json()
        assert client.get(path, headers=alice, params={"mode": "other"}).status_code == 422


def _tie_rejection(ready: ReadyCatalog, asset_id: str) -> None:
    """A rejection at the verified row's checked_at, leaving the asset READY."""
    with psycopg.connect(ready.database.migration_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO rights_evidence (
                source_record_id, asset_id, rights_type, verification_status,
                verification_method, checked_at
            )
            SELECT evidence.source_record_id, evidence.asset_id, %s, %s, %s, evidence.checked_at
            FROM rights_evidence AS evidence
            JOIN assets ON assets.source_record_id = evidence.source_record_id
            WHERE assets.id = %s
            ORDER BY evidence.checked_at DESC
            LIMIT 1
            """,
            (
                catalog.RIGHTS_REJECTED,
                catalog.VERIFICATION_REJECTED,
                catalog.METHOD_OPERATOR_REVIEW,
                UUID(asset_id),
            ),
        )


def test_tied_rejections_hide_candidates_and_pins(published: ReadyCatalog) -> None:
    settings = _settings(published)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        public = [item["id"] for item in client.get("/api/v1/assets").json()["items"]]
        kept, tied_pin, tied_candidate = public[0], public[1], public[2]
        board_id = _board(client, alice, kept, tied_pin)
        before = _recommend(client, alice, board_id)
        assert tied_candidate in _ids(before)

        _tie_rejection(published, tied_pin)
        _tie_rejection(published, tied_candidate)
        for mode in ("cohesive", "complement"):
            body = _recommend(client, alice, board_id, mode=mode)
            assert body["profile_pin_count"] == 1
            assert body["ignored_pin_count"] == 1
            assert {kept, tied_pin, tied_candidate}.isdisjoint(_ids(body))


def test_ineligible_and_unembedded_pins_never_shape_or_reappear(
    published: ReadyCatalog,
) -> None:
    settings = _settings(published)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        public = [item["id"] for item in client.get("/api/v1/assets").json()["items"]]
        kept, withdrawn, candidate = public[0], public[1], public[2]
        board_id = _board(client, alice, kept, withdrawn)

        _reject(published, withdrawn)
        _reject(published, candidate)
        body = _recommend(client, alice, board_id)
        assert body["profile_pin_count"] == 1
        assert body["ignored_pin_count"] == 1
        assert {kept, withdrawn, candidate}.isdisjoint(_ids(body))
        assert all(item["closest_pin_id"] == kept for item in body["items"])

        # Restored rights make the pin public again; it is saved, so it shapes
        # the profile and is still never recommended back.
        _restore(published, withdrawn)
        restored = _recommend(client, alice, board_id)
        assert restored["profile_pin_count"] == 2
        assert restored["ignored_pin_count"] == 0
        assert {kept, withdrawn, candidate}.isdisjoint(_ids(restored))

        # A public pin without an active-model vector is ignored, and an
        # otherwise empty board falls back to browse without repeating it.
        with psycopg.connect(published.database.migration_url, autocommit=True) as connection:
            connection.execute(
                f"DELETE FROM {catalog.EMBEDDINGS_TABLE} WHERE asset_id = %s", (UUID(kept),)
            )
        unembedded = _board(client, alice, kept)
        fallback = _recommend(client, alice, unembedded)
        assert fallback["fallback"] == "browse"
        assert fallback["ignored_pin_count"] == 1
        assert kept not in _ids(fallback)
        assert candidate not in _ids(fallback)
        assert all(item["score"] is None for item in fallback["items"])
