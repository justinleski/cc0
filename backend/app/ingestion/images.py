"""Bounded decode, SHA-256, and thumbnail generation for verified CC0 bytes.

Decoding untrusted input is the riskiest step in the pipeline, so the pixel
count is checked from the header before any pixels are loaded, and only three
container formats are accepted.
"""

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.ingestion.errors import PermanentFailure

# Declared content type must agree with what the decoder actually finds.
FORMAT_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
THUMBNAIL_CONTENT_TYPE = "image/webp"
THUMBNAIL_EXTENSION = "webp"


@dataclass(frozen=True)
class ProcessedImage:
    sha256: str
    mime_type: str
    extension: str
    width: int
    height: int
    original: bytes
    thumbnail: bytes


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def process(
    body: bytes,
    declared_content_type: str,
    *,
    max_pixels: int,
    thumbnail_max_pixels: int,
) -> ProcessedImage:
    """Verify, measure, and derive a thumbnail. Raises PermanentFailure."""
    try:
        with Image.open(BytesIO(body)) as probe:
            image_format = probe.format
            width, height = probe.size
    except UnidentifiedImageError as exc:
        raise PermanentFailure("undecodable_image", type(exc).__name__) from exc
    except OSError as exc:
        raise PermanentFailure("truncated_image", type(exc).__name__) from exc

    mime_type = FORMAT_TYPES.get(image_format or "")
    if mime_type is None:
        raise PermanentFailure("unsupported_image_format", image_format or "unknown")
    if mime_type != declared_content_type:
        # A JPEG served as image/png is a provider or proxy bug, and the stored
        # mime type has to match the bytes we keep.
        raise PermanentFailure("content_type_mismatch", f"{declared_content_type}!={mime_type}")
    if width <= 0 or height <= 0:
        raise PermanentFailure("empty_image_dimensions")
    # Header-only guard against a decompression bomb: refuse before loading.
    if width * height > max_pixels:
        raise PermanentFailure("image_too_many_pixels", f"{width}x{height}")

    try:
        with Image.open(BytesIO(body)) as image:
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail(
                (thumbnail_max_pixels, thumbnail_max_pixels),
                Image.Resampling.LANCZOS,
            )
            buffer = BytesIO()
            thumbnail.save(buffer, format="WEBP", quality=82, method=4)
    except OSError as exc:
        raise PermanentFailure("thumbnail_failed", type(exc).__name__) from exc

    return ProcessedImage(
        sha256=digest(body),
        mime_type=mime_type,
        extension=EXTENSIONS[mime_type],
        width=width,
        height=height,
        original=body,
        thumbnail=buffer.getvalue(),
    )
