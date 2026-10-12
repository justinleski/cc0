"""Bounded decoding, digests, and content-addressed keys."""

import hashlib
import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image

from app.ingestion import images
from app.ingestion.errors import PermanentFailure
from app.integrations.object_store import ORIGINAL_PREFIX, THUMBNAIL_PREFIX, content_key


def _jpeg(width: int = 900, height: int = 600) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _png(width: int = 64, height: int = 32) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def _png_header_claiming(width: int, height: int) -> bytes:
    """A few hundred bytes that declare an enormous image, with no pixels."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def test_original_is_hashed_and_thumbnail_is_bounded_webp():
    body = _jpeg()
    processed = images.process(
        body,
        "image/jpeg",
        max_pixels=50_000_000,
        thumbnail_max_pixels=512,
    )
    assert processed.sha256 == hashlib.sha256(body).hexdigest()
    assert (processed.width, processed.height) == (900, 600)
    assert processed.original is body
    with Image.open(BytesIO(processed.thumbnail)) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert max(thumbnail.size) == 512
        # Aspect ratio is preserved rather than squared off.
        assert thumbnail.size == (512, 341)


def test_declared_type_must_match_the_decoded_format():
    with pytest.raises(PermanentFailure, match="content_type_mismatch"):
        images.process(_png(), "image/jpeg", max_pixels=50_000_000, thumbnail_max_pixels=512)


def test_undecodable_and_truncated_bodies_are_permanent():
    with pytest.raises(PermanentFailure, match="undecodable_image"):
        images.process(
            b"<html>not an image</html>",
            "image/jpeg",
            max_pixels=50_000_000,
            thumbnail_max_pixels=512,
        )
    with pytest.raises(PermanentFailure, match="undecodable_image|truncated_image"):
        images.process(_jpeg()[:40], "image/jpeg", max_pixels=50_000_000, thumbnail_max_pixels=512)


def test_pixel_count_is_checked_from_the_header_before_decoding():
    with pytest.raises(PermanentFailure, match="image_too_many_pixels"):
        images.process(_jpeg(), "image/jpeg", max_pixels=1000, thumbnail_max_pixels=512)


def test_a_decompression_bomb_is_a_permanent_failure_not_a_crash():
    """Pillow's own ceiling is not an OSError, so it would escape the pipeline.

    A header claiming far more pixels than Pillow allows raises before any are
    loaded. Uncaught, it would kill the worker and leave the job leased.
    """
    bomb = _png_header_claiming(40_000, 40_000)
    with pytest.raises(PermanentFailure, match="image_too_many_pixels"):
        images.process(bomb, "image/png", max_pixels=50_000_000, thumbnail_max_pixels=512)


def test_keys_are_content_addressed_and_fanned_out():
    digest = "a" * 64
    assert content_key(ORIGINAL_PREFIX, digest, "jpg") == f"originals/aa/aa/{digest}.jpg"
    assert content_key(THUMBNAIL_PREFIX, digest, "webp") == f"thumbnails/aa/aa/{digest}.webp"


def test_identical_bytes_produce_one_key_so_storage_is_reused():
    body = _jpeg(100, 100)
    first = images.process(body, "image/jpeg", max_pixels=50_000_000, thumbnail_max_pixels=512)
    second = images.process(body, "image/jpeg", max_pixels=50_000_000, thumbnail_max_pixels=512)
    assert first.sha256 == second.sha256
    # Shared storage is not a claim that two records are the same work: the
    # asset rows stay separate and each keeps its own rights history.
    assert content_key(ORIGINAL_PREFIX, first.sha256, first.extension) == content_key(
        ORIGINAL_PREFIX, second.sha256, second.extension
    )
