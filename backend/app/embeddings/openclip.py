"""Optional OpenCLIP loader. Importing this module does not import torch.

The checkpoint name comes from configuration. `unpinned` refuses to download
weights, so a default environment cannot silently fetch a model. Output width
is checked against the vector column rather than padded.
"""

import io
import threading
from pathlib import Path

UNPINNED_VERSION = "unpinned"


class OpenClipProvider:
    """One resident model for the process. Inference is serialized on a lock."""

    def __init__(
        self,
        *,
        model_key: str,
        model_version: str,
        dimensions: int,
        cache_dir: Path,
    ) -> None:
        self.model_key = model_key
        self.model_version = model_version
        self.dimensions = dimensions
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None

    def prepare(self) -> None:
        self._ensure_loaded()
        vector = self.embed_text("dimension check")
        if len(vector) != self.dimensions:
            raise ValueError(
                f"{self.model_key} {self.model_version} emitted {len(vector)} "
                f"dimensions, and the column is vector({self.dimensions})"
            )

    def embed_image(self, image_bytes: bytes) -> list[float]:
        # The lock is the process-wide bound: one encode runs, and the caller
        # holds it only for inference, after releasing any pooled connection.
        with self._lock:
            self._ensure_loaded()
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            tensor = self._preprocess(image).unsqueeze(0)
            with self._torch.no_grad():
                features = self._model.encode_image(tensor)
                features = features / features.norm(dim=-1, keepdim=True)
            return [float(value) for value in features[0].detach().float().tolist()]

    def embed_text(self, text: str) -> list[float]:
        from app.embeddings.provider import normalize_text

        with self._lock:
            self._ensure_loaded()
            tokens = self._tokenizer([normalize_text(text)])
            with self._torch.no_grad():
                features = self._model.encode_text(tokens)
                features = features / features.norm(dim=-1, keepdim=True)
            return [float(value) for value in features[0].detach().float().tolist()]

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if self.model_version == UNPINNED_VERSION:
            raise RuntimeError(
                "MODEL_VERSION is unpinned. Set it to a ViT-B checkpoint, "
                "for example laion2b_s34b_b79k, before loading OpenCLIP."
            )
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise RuntimeError(
                'OpenCLIP is not installed. Install it with: pip install -e "backend[embeddings]"'
            ) from exc
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        model, _, preprocess = open_clip.create_model_and_transforms(
            self.model_key,
            pretrained=self.model_version,
            cache_dir=str(self._cache_dir),
        )
        model.eval()
        self._torch = torch
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(self.model_key)
