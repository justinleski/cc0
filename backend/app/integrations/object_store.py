"""S3-compatible object access for both the API and the ingestion worker.

The same client code runs against RustFS locally and S3 on AWS, with path-style
addressing because RustFS requires it and S3 still accepts it ([ADR-0001]).

Originals and thumbnails are content-addressed: the key contains the SHA-256 of
the original bytes. A single PUT is atomic, so an object at that key is either
absent or complete, and the digest is also written as object metadata so a
resuming worker can prove the bytes match before it skips a download.

[ADR-0001]: ../../../docs/decisions/0001-object-storage.md
"""

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings

SHA256_METADATA_KEY = "sha256"

ORIGINAL_PREFIX = "originals"
THUMBNAIL_PREFIX = "thumbnails"


class ObjectStoreUnavailable(Exception):
    """The object store could not be reached or refused the call."""


def create_s3_client(
    settings: Settings, timeout_seconds: float | None = None, endpoint: str | None = None
):
    timeout = settings.readiness_timeout_seconds if timeout_seconds is None else timeout_seconds
    return boto3.client(
        "s3",
        endpoint_url=endpoint or settings.object_store_endpoint,
        aws_access_key_id=settings.object_store_access_key,
        aws_secret_access_key=settings.object_store_secret_key,
        region_name=settings.object_store_region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            connect_timeout=timeout,
            read_timeout=timeout,
            retries={"max_attempts": 1},
        ),
    )


def content_key(prefix: str, sha256: str, extension: str) -> str:
    """Fan out by digest so one prefix does not collect every object."""
    return f"{prefix}/{sha256[:2]}/{sha256[2:4]}/{sha256}.{extension}"


class ObjectStore:
    """Writes, reads and existence checks. Only export cleanup deletes an object."""

    def __init__(self, client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def stored_digest(self, key: str) -> str | None:
        """The recorded SHA-256 of an existing object, or None when absent."""
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (404, 403):
                return None
            raise ObjectStoreUnavailable(type(exc).__name__) from exc
        except BotoCoreError as exc:
            raise ObjectStoreUnavailable(type(exc).__name__) from exc
        return response.get("Metadata", {}).get(SHA256_METADATA_KEY)

    def get_bytes(self, key: str) -> bytes | None:
        """The object body, or None when the key is absent.

        Embed reads the original already stored by ingest. A missing key is a
        permanent failure for that job, not a reason to download the image again.
        A timeout or client error while reading the body is ObjectStoreUnavailable,
        the same as a failed GET, and the body is closed either way.
        """
        body = None
        try:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status in (404, 403):
                    return None
                raise ObjectStoreUnavailable(type(exc).__name__) from exc
            except BotoCoreError as exc:
                raise ObjectStoreUnavailable(type(exc).__name__) from exc
            body = response["Body"]
            try:
                return body.read()
            except (BotoCoreError, ClientError) as exc:
                raise ObjectStoreUnavailable(type(exc).__name__) from exc
        finally:
            if body is not None:
                body.close()

    def put(self, key: str, body: bytes, content_type: str, sha256: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata={SHA256_METADATA_KEY: sha256},
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreUnavailable(type(exc).__name__) from exc

    def put_file(self, key: str, fileobj, content_type: str, sha256: str, size: int) -> None:
        """Upload a finished local file. An object appears only once it is complete.

        S3 multipart uploads are invisible until completed, and a single PUT is
        atomic, so a reader never sees a partial archive. The length is checked
        afterwards because the caller publishes the key on the strength of it.
        """
        try:
            self._client.upload_fileobj(
                fileobj,
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type, "Metadata": {SHA256_METADATA_KEY: sha256}},
            )
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreUnavailable(type(exc).__name__) from exc
        if head.get("ContentLength") != size:
            raise ObjectStoreUnavailable("size_mismatch")

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreUnavailable(type(exc).__name__) from exc

    def list_objects(self, prefix: str):
        """Yield (key, last_modified) under a prefix, a page at a time."""
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    yield item["Key"], item["LastModified"]
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreUnavailable(type(exc).__name__) from exc

    def presigned_get(self, key: str, expires_seconds: int, filename: str) -> str:
        """A short-lived GET URL. Signing is local; it makes no request.

        Build the client from the browser-reachable endpoint, or the host in the
        signature will not match the one the browser calls.
        """
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": "application/zip",
            },
            ExpiresIn=expires_seconds,
        )
