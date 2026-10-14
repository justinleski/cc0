"""Image and text embeddings.

The checkpoint is configuration, not a pinned download. Tests and CI use the
deterministic fake. OpenCLIP is an optional extra and a manual smoke command.
"""

from app.embeddings.provider import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    build_provider,
    cosine,
    normalize_text,
)

__all__ = [
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "build_provider",
    "cosine",
    "normalize_text",
]
