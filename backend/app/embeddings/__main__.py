"""Manual check for a real OpenCLIP checkpoint. CI does not run this.

Compares order, not bit-exact floats: a related pair must outrank an unrelated
one, and the width must match the vector column. GPU and CPU builds are not
expected to agree on the last bits.
"""

import io
import sys

from PIL import Image

from app.core.config import get_settings
from app.embeddings.provider import build_provider, cosine


def _solid(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (64, 64), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    settings = get_settings()
    if settings.embedding_provider != "openclip":
        print(
            "Set EMBEDDING_PROVIDER=openclip and MODEL_VERSION to a ViT-B checkpoint.",
            file=sys.stderr,
        )
        return 2
    provider = build_provider(settings)
    try:
        provider.prepare()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    blue = provider.embed_image(_solid((30, 80, 180)))
    coastal = provider.embed_text("a misty blue coastal painting")
    ledger = provider.embed_text("a spreadsheet of quarterly revenue")
    if not (len(blue) == len(coastal) == settings.model_dimensions):
        print(
            f"width {len(blue)} does not match MODEL_DIMENSIONS {settings.model_dimensions}",
            file=sys.stderr,
        )
        return 1
    related = cosine(blue, coastal)
    unrelated = cosine(blue, ledger)
    print(f"cosine related={related:.4f} unrelated={unrelated:.4f}")
    if related <= unrelated:
        print("related image/text pair did not outrank the unrelated text", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
