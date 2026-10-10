from contextlib import closing

import boto3
import psycopg
from botocore.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_running_stack_supports_vector_and_head_bucket(running_stack: Settings) -> None:
    settings = running_stack

    with psycopg.connect(settings.database_url, connect_timeout=2) as connection:
        assert connection.execute("SELECT current_user").fetchone() == ("cc0_api",)
        assert connection.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector").fetchone() == (
            0.0,
        )

    with closing(
        boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint,
            aws_access_key_id=settings.object_store_access_key,
            aws_secret_access_key=settings.object_store_secret_key,
            region_name=settings.object_store_region,
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 0},
            ),
        )
    ) as client:
        assert settings.object_store_bucket == "cc0"
        response = client.head_bucket(Bucket=settings.object_store_bucket)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_health_and_ready_respond_against_the_running_stack(running_stack: Settings) -> None:
    with TestClient(create_app(running_stack)) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        ready = client.get("/api/v1/ready")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ok",
            "checks": {"postgres": "ok", "object_store": "ok"},
        }
