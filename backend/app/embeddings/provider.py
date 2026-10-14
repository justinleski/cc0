"""The embedding contract, and the deterministic provider tests run.

Image and text vectors share one space for a given model key and version.
Query vectors are not stored: a search encodes its text, ranks, and drops the
vector. The fake provider exists so CI and the default local run can assert
exact vectors they own, without downloading weights or comparing GPU floats.

Its stored identity is `fake` / `deterministic-v1`. MODEL_KEY and MODEL_VERSION
name an OpenCLIP checkpoint only, so a fake row cannot occupy that primary key.
"""

import hashlib
import math
import threading
from typing import Protocol

from app.core.config import Settings
from app.db.catalog import VECTOR_DIMENSIONS

# Distinct from every OpenCLIP checkpoint name. Search and embed read this pair
# from the provider, and the worker has no UPDATE on asset_embeddings, so a
# fake vector stored under a real checkpoint name could not be replaced.
FAKE_MODEL_KEY = "fake"
FAKE_MODEL_VERSION = "deterministic-v1"


class EmbeddingProvider(Protocol):
    model_key: str
    model_version: str
    dimensions: int

    def prepare(self) -> None:
        """Load weights when this provider has any. The fake provider does not."""

    def embed_image(self, image_bytes: bytes) -> list[float]:
        """L2-normalized image vector. Only verified CC0 bytes should be passed."""

    def embed_text(self, text: str) -> list[float]:
        """L2-normalized text vector in the same space as embed_image."""


def normalize_text(value: str) -> str:
    """Casefold and collapse whitespace so the same words encode once."""
    return " ".join(value.casefold().split())


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity. Both inputs are expected to be L2-normalized."""
    if len(left) != len(right) or not left:
        raise ValueError("vectors must be the same non-zero length")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise ValueError("embedding norm is zero")
    return [value / norm for value in values]


def deterministic_vector(seed: bytes, dimensions: int) -> list[float]:
    """Expand a seed into a unit vector. The same seed always returns the same floats."""
    raw = bytearray()
    counter = 0
    while len(raw) < dimensions * 4:
        raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    values: list[float] = []
    for index in range(dimensions):
        chunk = raw[index * 4 : (index + 1) * 4]
        unit = int.from_bytes(chunk, "big") / 4294967295
        values.append(unit * 2 - 1)
    return _l2_normalize(values)


class FakeEmbeddingProvider:
    """Deterministic stand-in. Results from this provider are not semantic search.

    One encode runs at a time. Callers encode before they check out a pooled
    connection, so the wait does not hold a database connection.
    """

    def __init__(self, dimensions: int) -> None:
        self.model_key = FAKE_MODEL_KEY
        self.model_version = FAKE_MODEL_VERSION
        self.dimensions = dimensions
        self._encode_slots = threading.BoundedSemaphore(1)

    def prepare(self) -> None:
        vector = self.embed_text("dimension check")
        if len(vector) != self.dimensions:
            raise ValueError(
                f"fake embedding width {len(vector)} != configured {self.dimensions}"
            )

    def embed_image(self, image_bytes: bytes) -> list[float]:
        with self._encode_slots:
            digest = hashlib.sha256(image_bytes).digest()
            return deterministic_vector(b"image\0" + digest, self.dimensions)

    def embed_text(self, text: str) -> list[float]:
        with self._encode_slots:
            normalized = normalize_text(text)
            return deterministic_vector(b"text\0" + normalized.encode(), self.dimensions)


def build_provider(settings: Settings) -> EmbeddingProvider:
    """The process's one provider. OpenCLIP is imported only when selected."""
    if settings.model_dimensions != VECTOR_DIMENSIONS:
        raise ValueError(
            f"MODEL_DIMENSIONS must be {VECTOR_DIMENSIONS}; "
            "a different width needs a new vector column"
        )
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(settings.model_dimensions)
    from app.embeddings.openclip import OpenClipProvider

    return OpenClipProvider(
        model_key=settings.model_key,
        model_version=settings.model_version,
        dimensions=settings.model_dimensions,
        cache_dir=settings.embedding_cache_path,
    )
