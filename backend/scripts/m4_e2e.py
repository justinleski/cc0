"""Run the complete M4 journey against real processes and a throwaway stack slice.

    cd backend && .venv/bin/python scripts/m4_e2e.py

It creates a temporary database and bucket on the local Postgres and RustFS
(`docker compose up -d postgres rustfs rustfs-init`), migrates, ingests the
fixtures, starts the API, and walks board -> pins -> both recommendation modes ->
export -> worker -> download over HTTP, verifying the archive against the
catalog. Nothing in your development database or bucket is read or written.
Exit status is non-zero on the first failed check.
"""

import csv
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4

import boto3
import psycopg
from botocore.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402


def _swap(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Journey:
    def __init__(self) -> None:
        self.settings = Settings()
        suffix = uuid4().hex[:10]
        self.database = f"cc0_m4_e2e_{suffix}"
        self.bucket = f"cc0-m4-e2e-{suffix}"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.checks = 0
        s = self.settings
        self.env = {
            **os.environ,
            "MIGRATION_DATABASE_URL": _swap(s.migration_database_url, self.database),
            "DATABASE_URL": _swap(s.database_url, self.database),
            "WORKER_DATABASE_URL": _swap(s.worker_database_url, self.database),
            "OBJECT_STORE_BUCKET": self.bucket,
            "INGEST_FIXTURE_DIR": str(BACKEND / "fixtures" / "met"),
            "APP_ENV": "local",
            "AUTH_MODE": "local",
            "AUTH_LOCAL_SIGNING_KEY": secrets.token_hex(32),
            "CURSOR_SIGNING_KEY": secrets.token_hex(32),
        }
        self.server: subprocess.Popen | None = None
        self.s3 = boto3.client(
            "s3",
            endpoint_url=s.object_store_endpoint,
            aws_access_key_id=s.object_store_access_key,
            aws_secret_access_key=s.object_store_secret_key,
            region_name=s.object_store_region,
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    def check(self, condition: bool, message: str) -> None:
        self.checks += 1
        if not condition:
            raise SystemExit(f"FAILED: {message}")
        print(f"  ok  {message}")

    def run(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        done = subprocess.run(
            [sys.executable, *args], cwd=BACKEND, env=self.env, capture_output=True, text=True
        )
        if done.returncode not in ok:
            raise SystemExit(
                f"FAILED: {' '.join(args)} exited {done.returncode}\n{done.stderr[-2000:]}"
            )
        return done.stdout.strip()

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def setup(self) -> None:
        admin = _swap(self.settings.migration_database_url, "postgres")
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        self.s3.create_bucket(Bucket=self.bucket)
        self.run("-m", "alembic", "upgrade", "head")
        # Exit 1 means a fixture was dead-lettered, which the failure fixtures do on purpose.
        self.run("-m", "app.worker", "ingest", ok=(0, 1))
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=BACKEND,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            try:
                if self.call("GET", "/api/v1/ready")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise SystemExit("FAILED: API did not become ready")

    def teardown(self) -> None:
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=10)
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    self.s3.delete_object(Bucket=self.bucket, Key=item["Key"])
            self.s3.delete_bucket(Bucket=self.bucket)
        except Exception as error:  # cleanup must not hide the real result
            print(f"warning: bucket cleanup failed: {error}")
        admin = _swap(self.settings.migration_database_url, "postgres")
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.database)
                )
            )

    def journey(self) -> None:
        alice = self.run("-m", "app.auth", "token", "alice")
        bob = self.run("-m", "app.auth", "token", "bob")
        status, listing = self.call("GET", "/api/v1/assets?limit=50")
        catalog_ids = [item["id"] for item in listing["items"]]
        self.check(
            status == 200 and len(catalog_ids) >= 10,
            f"{len(catalog_ids)} verified CC0 assets are searchable",
        )

        _, board = self.call("POST", "/api/v1/boards", alice, {"name": "M4 journey"})
        board_id = board["id"]
        path = f"/api/v1/boards/{board_id}"
        status, body = self.call("POST", path + "/exports", alice)
        self.check(status == 409, "an empty board cannot be exported")

        pins = catalog_ids[:3]
        for asset_id in pins:
            self.call("POST", path + "/items", alice, {"asset_id": asset_id})

        suggestions = {}
        for mode in ("cohesive", "complement"):
            status, body = self.call("GET", f"{path}/recommendations?mode={mode}&limit=20", alice)
            ids = [item["id"] for item in body["items"]] if status == 200 else []
            self.check(status == 200 and ids, f"'{mode}' recommendations returned {len(ids)} works")
            self.check(
                not set(ids) & set(pins) and set(ids) <= set(catalog_ids),
                f"'{mode}' never repeats a pin and stays inside the verified catalog",
            )
            suggestions[mode] = ids
        taken = suggestions["cohesive"][0]
        self.call("POST", path + "/items", alice, {"asset_id": taken})
        _, after = self.call("GET", f"{path}/recommendations?mode=cohesive&limit=20", alice)
        after_ids = [item["id"] for item in after["items"]]
        self.check(
            taken not in after_ids and after_ids != suggestions["cohesive"],
            "pinning a suggestion changes the next recommendations",
        )
        self.check(
            self.call("GET", f"{path}/recommendations", bob)[0] == 404,
            "another user cannot read this board's recommendations",
        )
        self.check(
            self.call("POST", path + "/exports", bob)[0] == 404,
            "another user cannot export this board",
        )

        status, export = self.call("POST", path + "/exports", alice)
        self.check(status == 202 and export["status"] == "QUEUED", "export accepted (202, QUEUED)")
        export_id = export["id"]
        self.check(
            self.call("GET", f"/api/v1/exports/{export_id}", bob)[0] == 404,
            "another user cannot read the export",
        )
        self.run("-m", "app.worker", "exports")
        status, ready = self.call("GET", f"/api/v1/exports/{export_id}", alice)
        self.check(
            ready["status"] == "READY" and ready["download_url"], "worker published the export"
        )
        with urllib.request.urlopen(ready["download_url"], timeout=30) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        manifest = json.loads(archive.read("manifest.json"))
        rows = list(csv.DictReader(io.StringIO(archive.read("attribution.csv").decode())))
        expected = pins + [taken]
        self.check(
            [a["asset_id"] for a in manifest["assets"]] == expected
            and [r["asset_id"] for r in rows] == expected,
            "manifest and attribution list exactly the pinned assets in order",
        )
        self.check(
            all(
                hashlib.sha256(archive.read(a["filename"])).hexdigest() == a["sha256"]
                for a in manifest["assets"]
            ),
            "every image matches its manifest SHA-256",
        )
        self.check(
            all(
                a["rights"]["type"] == "CC0"
                and a["rights"]["evidence_url"]
                and a["source"]["landing_url"]
                for a in manifest["assets"]
            ),
            "rights evidence and source landing URL accompany every image",
        )


def main() -> int:
    journey = Journey()
    try:
        print(f"Throwaway database {journey.database}, bucket {journey.bucket}, API {journey.base}")
        journey.setup()
        journey.journey()
    finally:
        journey.teardown()
    print(f"\nM4 journey passed ({journey.checks} checks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
