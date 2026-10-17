"""Board HTTP operations against migrated PostgreSQL and real public catalog rows."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

from app.auth.local import identity, issue_token
from app.boards.models import BoardConflict
from app.boards.repository import BoardRepository
from app.boards.service import BoardService
from app.core.config import Settings
from app.db import catalog
from app.main import create_app
from tests.integration.test_search import (
    FIELD,
    HARBOR,
    REEDS,
    ReadyCatalog,
    _publish,
    ready_catalog,
)

# Importing the fixture above deliberately reuses its isolated DB and bucket.
_ = ready_catalog
KEY = "integration-only-local-signing-key-32-bytes"


@pytest.fixture(autouse=True)
def empty_private_tables(ready_catalog: ReadyCatalog) -> None:
    # The imported catalog fixture clears catalog tables; private tables need
    # their own reset because this module reuses one migrated throwaway DB.
    with psycopg.connect(ready_catalog.database.migration_url) as connection:
        connection.execute("TRUNCATE board_create_operations, board_items, boards, users CASCADE")


def _settings(ready: ReadyCatalog) -> Settings:
    return ready.settings.model_copy(
        update={"app_env": "local", "auth_mode": "local", "auth_local_signing_key": SecretStr(KEY)}
    )


def _headers(settings: Settings, account: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(settings, account)}"}


def _public_ids(client: TestClient) -> list[str]:
    return [item["id"] for item in client.get("/api/v1/assets").json()["items"]]


def test_create_key_retries_survive_edits_and_deletes_without_cross_owner_leaks(
    ready_catalog: ReadyCatalog,
) -> None:
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        alice, bob = _headers(settings, "alice"), _headers(settings, "bob")
        key = str(uuid4())
        alice["Idempotency-Key"] = key
        bob["Idempotency-Key"] = key
        payload = {"name": "Same name", "description": "first"}
        first = client.post("/api/v1/boards", headers=alice, json=payload)
        assert first.status_code == 201, first.text
        board_id = first.json()["id"]
        again = client.post("/api/v1/boards", headers=alice, json=payload)
        assert again.status_code == 201, again.text
        assert again.json() == first.json()
        assert (
            client.post(
                "/api/v1/boards", headers=alice, json={**payload, "description": "changed"}
            ).status_code
            == 409
        )
        changed = client.patch(
            f"/api/v1/boards/{board_id}", headers=alice, json={"name": "Edited"}
        )
        assert changed.status_code == 200
        edited_retry = client.post("/api/v1/boards", headers=alice, json=payload)
        assert edited_retry.status_code == 201
        assert edited_retry.json()["id"] == board_id
        assert edited_retry.json()["name"] == "Edited"

        other_owner = client.post("/api/v1/boards", headers=bob, json=payload)
        assert other_owner.status_code == 201
        assert other_owner.json()["id"] != board_id
        assert client.get(f"/api/v1/boards/{board_id}", headers=bob).status_code == 404
        # The defective API accepted client board IDs and returned 409 for a
        # known foreign ID but 201 for an unused ID. Both are invalid bodies now.
        known_id = client.post(
            "/api/v1/boards", headers=bob, json={**payload, "id": board_id}
        )
        unknown_id = client.post(
            "/api/v1/boards", headers=bob, json={**payload, "id": str(uuid4())}
        )
        assert known_id.status_code == unknown_id.status_code == 422

        alice["Idempotency-Key"] = str(uuid4())
        distinct = client.post("/api/v1/boards", headers=alice, json=payload)
        assert distinct.status_code == 201
        assert distinct.json()["id"] != board_id
        alice.pop("Idempotency-Key")
        unkeyed = client.post("/api/v1/boards", headers=alice, json=payload)
        assert unkeyed.status_code == 201
        assert unkeyed.json()["id"] not in {board_id, distinct.json()["id"]}
        listed = client.get("/api/v1/boards", headers=alice).json()["items"]
        assert {item["id"] for item in listed} == {
            board_id, distinct.json()["id"], unkeyed.json()["id"]
        }

        alice["Idempotency-Key"] = key
        assert client.delete(f"/api/v1/boards/{board_id}", headers=alice).status_code == 204
        deleted_retry = client.post("/api/v1/boards", headers=alice, json=payload)
        assert deleted_retry.status_code == 404
        assert client.get(f"/api/v1/boards/{board_id}", headers=alice).status_code == 404
        assert client.post(
            "/api/v1/boards", headers=alice, json={**payload, "name": "Changed"}
        ).status_code == 409


def test_concurrent_create_retries_use_one_board(ready_catalog: ReadyCatalog) -> None:
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        owner = identity("alice").id
        headers = _headers(settings, "alice")
        assert client.get("/api/v1/boards", headers=headers).status_code == 200
        key = str(uuid4())
        service = BoardService(BoardRepository(client.app.state.db_engine))
        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(
                lambda _: service.create(owner, "Concurrent retry", "", key), range(2)
            ))
        assert created[0] == created[1]
        assert len(client.get("/api/v1/boards", headers=headers).json()["items"]) == 1

        conflicting_key = str(uuid4())

        def create_conflicting(name: str) -> UUID | str:
            try:
                return service.create(owner, name, "", conflicting_key).id
            except BoardConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create_conflicting, ("First", "Second")))
        assert outcomes.count("conflict") == 1
        assert len([item for item in outcomes if isinstance(item, UUID)]) == 1
        assert len(client.get("/api/v1/boards", headers=headers).json()["items"]) == 2

        # A failed board insert must roll back its operation reservation too.
        rollback_key = str(uuid4())
        repository = BoardRepository(client.app.state.db_engine)
        with pytest.raises(IntegrityError):
            repository.create(owner, " ", "", rollback_key, b"x" * 32)
        recovered = service.create(owner, "Valid", "", rollback_key)
        assert recovered.name == "Valid"


def test_board_crud_owner_isolation_and_inputs(ready_catalog: ReadyCatalog) -> None:
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        alice, bob = _headers(settings, "alice"), _headers(settings, "bob")
        unauthenticated = client.post("/api/v1/boards", json={"name": "Private"})
        assert unauthenticated.status_code == 401
        created = client.post(
            "/api/v1/boards",
            headers=alice,
            json={"name": "  Private  ", "description": "first"},
        )
        assert created.status_code == 201, created.text
        board = created.json()
        board_id = board["id"]
        assert board["name"] == "Private"
        assert board["items"] == []
        assert [
            item["id"] for item in client.get("/api/v1/boards", headers=alice).json()["items"]
        ] == [board_id]
        assert client.get("/api/v1/boards", headers=bob).json()["items"] == []
        missing = uuid4()
        operations = [
            ("get", f"/api/v1/boards/{board_id}", None),
            ("patch", f"/api/v1/boards/{board_id}", {"name": "Stolen"}),
            ("delete", f"/api/v1/boards/{board_id}", None),
            ("post", f"/api/v1/boards/{board_id}/items", {"asset_id": str(uuid4())}),
            ("put", f"/api/v1/boards/{board_id}/items/order", {"asset_ids": []}),
            ("patch", f"/api/v1/boards/{board_id}/items/{uuid4()}", {"notes": "stolen"}),
            ("delete", f"/api/v1/boards/{board_id}/items/{uuid4()}", None),
        ]
        for method, path, body in operations:
            response = client.request(method.upper(), path, headers=bob, json=body)
            assert response.status_code == 404, (method, response.text)
        for board_id_to_check in [board_id, str(missing)]:
            response = client.get(f"/api/v1/boards/{board_id_to_check}", headers=bob)
            assert response.status_code == 404
            assert response.json() == {"detail": "board not found"}
        assert (
            client.patch(
                f"/api/v1/boards/{board_id}", headers=alice, json={"name": None}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/boards",
                headers=alice,
                json={"name": "Other", "owner_user_id": str(uuid4())},
            ).status_code
            == 422
        )
        edited = client.patch(
            f"/api/v1/boards/{board_id}",
            headers=alice,
            json={"name": "Updated", "description": "notes"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "Updated"
        assert edited.json()["description"] == "notes"
        assert client.delete(f"/api/v1/boards/{board_id}", headers=alice).status_code == 204
        assert client.get(f"/api/v1/boards/{board_id}", headers=alice).status_code == 404


def test_pin_eligibility_notes_reorder_and_suppression(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR, FIELD, REEDS])
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        assets = _public_ids(client)
        created = client.post(
            "/api/v1/boards", headers=alice, json={"name": "Selected"}
        ).json()
        path = f"/api/v1/boards/{created['id']}"
        for position, asset_id in enumerate(assets):
            response = client.post(
                f"{path}/items",
                headers=alice,
                json={"asset_id": asset_id, "notes": f"note {position}"},
            )
            assert response.status_code == 201, response.text
            assert [item["asset_id"] for item in response.json()["items"]] == assets[: position + 1]
        assert (
            client.post(f"{path}/items", headers=alice, json={"asset_id": assets[0]}).status_code
            == 409
        )
        assert (
            client.post(f"{path}/items", headers=alice, json={"asset_id": str(uuid4())}).status_code
            == 409
        )
        assert ready_catalog.ingest.service.enqueue(["100005"]) == {}
        ready_catalog.ingest.drain()
        stored_id = ready_catalog.scalar(
            "SELECT id FROM assets WHERE status = :stored", stored=catalog.ASSET_STORED
        )
        assert isinstance(stored_id, UUID)
        assert (
            client.post(
                f"{path}/items", headers=alice, json={"asset_id": str(stored_id)}
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"{path}/items/order", headers=alice, json={"asset_ids": [assets[0], assets[0]]}
            ).status_code
            == 400
        )
        assert (
            client.put(
                f"{path}/items/order", headers=alice, json={"asset_ids": assets[:2]}
            ).status_code
            == 409
        )
        reversed_ids = list(reversed(assets))
        reordered = client.put(
            f"{path}/items/order", headers=alice, json={"asset_ids": reversed_ids}
        )
        assert reordered.status_code == 200, reordered.text
        assert [item["asset_id"] for item in reordered.json()["items"]] == reversed_ids
        assert [item["position"] for item in reordered.json()["items"]] == list(range(3))
        updated = client.patch(
            f"{path}/items/{assets[0]}", headers=alice, json={"notes": "new note"}
        )
        assert updated.status_code == 200
        assert (
            next(item for item in updated.json()["items"] if item["asset_id"] == assets[0])["notes"]
            == "new note"
        )
        source_id = ready_catalog.scalar(
            "SELECT source_record_id FROM assets WHERE id = :asset_id", asset_id=UUID(assets[1])
        )
        assert isinstance(source_id, UUID)
        ready_catalog.ingestion_repository.record_superseding_rights(
            source_id,
            rights_type=catalog.RIGHTS_REJECTED,
            verification_status=catalog.VERIFICATION_REJECTED,
            verification_method=catalog.METHOD_OPERATOR_REVIEW,
            evidence_url=None,
            raw_statement="rights withdrawn",
            decision_reason="operator_rejection",
        )
        visible = client.get(path, headers=alice).json()["items"]
        assert [item["asset_id"] for item in visible] == [
            item for item in reversed_ids if item != assets[1]
        ]
        assert [item["position"] for item in visible] == [0, 1]
        assert (
            client.post(f"{path}/items", headers=alice, json={"asset_id": assets[1]}).status_code
            == 409
        )
        with psycopg.connect(ready_catalog.database.api_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM board_items WHERE board_id = %s", (UUID(created["id"]),)
                ).fetchone()[0]
                == 3
            )
        current = [item for item in reversed_ids if item != assets[1]]
        assert (
            client.put(
                f"{path}/items/order", headers=alice, json={"asset_ids": list(reversed(current))}
            ).status_code
            == 200
        )
        assert client.delete(f"{path}/items/{assets[1]}", headers=alice).status_code == 200
        assert client.delete(f"{path}/items/{assets[1]}", headers=alice).status_code == 404
        assert client.delete(f"{path}/items/{assets[0]}", headers=alice).status_code == 200
        remaining = client.get(path, headers=alice).json()["items"]
        assert [item["position"] for item in remaining] == [0]


def test_concurrent_board_modifications_serialize_and_rollback(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        assets = _public_ids(client)
        board_id = client.post(
            "/api/v1/boards", headers=alice, json={"name": "Concurrent"}
        ).json()["id"]
        path = f"/api/v1/boards/{board_id}"

        def add(asset_id):
            return client.post(f"{path}/items", headers=alice, json={"asset_id": asset_id})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(add, assets))
        assert [result.status_code for result in results] == [201, 201]
        items = client.get(path, headers=alice).json()["items"]
        assert {item["asset_id"] for item in items} == set(assets)
        assert [item["position"] for item in items] == [0, 1]
        assert (
            client.put(
                f"{path}/items/order", headers=alice, json={"asset_ids": [assets[0]]}
            ).status_code
            == 409
        )
        assert [item["asset_id"] for item in client.get(path, headers=alice).json()["items"]] == [
            item["asset_id"] for item in items
        ]
        # A deferred position constraint failure rolls back the whole reorder.
        with psycopg.connect(ready_catalog.database.api_url) as connection:
            with pytest.raises(psycopg.errors.UniqueViolation):
                connection.execute(
                    "UPDATE board_items SET position = 0 WHERE board_id = %s",
                    (UUID(board_id),),
                )
                connection.commit()
        assert [item["position"] for item in client.get(path, headers=alice).json()["items"]] == [
            0,
            1,
        ]


def test_duplicate_concurrent_pin_and_failed_write_rollback(ready_catalog: ReadyCatalog) -> None:
    _publish(ready_catalog, [HARBOR, FIELD])
    settings = _settings(ready_catalog)
    with TestClient(create_app(settings)) as client:
        alice = _headers(settings, "alice")
        assets = _public_ids(client)
        board_id = client.post(
            "/api/v1/boards", headers=alice, json={"name": "Atomic"}
        ).json()["id"]
        path = f"/api/v1/boards/{board_id}"

        def add_same(_index):
            return client.post(f"{path}/items", headers=alice, json={"asset_id": assets[0]})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(add_same, range(2)))
        assert sorted(response.status_code for response in results) == [201, 409]
        assert len(client.get(path, headers=alice).json()["items"]) == 1

        from sqlalchemy import event

        engine = client.app.state.db_engine

        def fail_touch(conn, cursor, statement, parameters, context, executemany):
            if "UPDATE boards SET updated_at" in statement:
                raise RuntimeError("injected write failure")

        event.listen(engine, "before_cursor_execute", fail_touch)
        try:
            # The repository transaction must roll back its preceding INSERT.
            repository = BoardRepository(engine)
            with pytest.raises(RuntimeError, match="injected write failure"):
                repository.change_item(identity("alice").id, UUID(board_id), UUID(assets[1]), "add")
        finally:
            event.remove(engine, "before_cursor_execute", fail_touch)
        assert [item["asset_id"] for item in client.get(path, headers=alice).json()["items"]] == [
            assets[0]
        ]


def test_board_persists_across_api_lifespans(ready_catalog: ReadyCatalog) -> None:
    settings = _settings(ready_catalog)
    alice = _headers(settings, "alice")
    alice["Idempotency-Key"] = str(uuid4())
    payload = {"name": "Persisted", "description": "survives restart"}
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/boards",
            headers=alice,
            json=payload,
        )
        assert response.status_code == 201
        board_id = response.json()["id"]
    with TestClient(create_app(settings)) as restarted:
        retry = restarted.post("/api/v1/boards", headers=alice, json=payload)
        assert retry.status_code == 201
        assert retry.json()["id"] == board_id
        saved = restarted.get(f"/api/v1/boards/{board_id}", headers=alice)
        assert saved.status_code == 200
        assert saved.json()["description"] == "survives restart"
        assert [
            board["id"] for board in restarted.get("/api/v1/boards", headers=alice).json()["items"]
        ] == [board_id]
