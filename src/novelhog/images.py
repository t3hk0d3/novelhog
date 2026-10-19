"""Image handling (spec sections 8.2 and 8.4)."""

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageSequence

JPEG_QUALITY = 90

_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}


class TooLarge(Exception):  # noqa: N818 - name fixed by the task contract
    """An image declares more pixels than allowed."""


@dataclass(frozen=True)
class FetchedImage:
    """A downloaded image: its bytes and the MIME type detected from its magic bytes."""

    data: bytes
    mime: str


def sniff(data: bytes) -> str | None:
    """Detect png, jpeg, gif or webp from magic bytes; anything else is None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_pixels(data: bytes, max_pixels: int) -> None:
    """Raise TooLarge when the header declares more than ``max_pixels``; never decodes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
    except Image.DecompressionBombError as exc:
        raise TooLarge(str(exc)) from exc  # noqa: TRY003
    if width * height > max_pixels:
        raise TooLarge(f"{width}x{height} exceeds {max_pixels} pixels")  # noqa: TRY003


def _is_animated(image: Image.Image) -> bool:
    return bool(getattr(image, "is_animated", False))


def downscale(data: bytes, mime: str, max_dimension: int) -> bytes:
    """Shrink so the longest side is at most ``max_dimension``, in the original format."""
    if max_dimension <= 0:
        return data
    with Image.open(io.BytesIO(data)) as image:
        if _is_animated(image) or max(image.size) <= max_dimension:
            return data
        image.load()
        resized = image.copy()
        resized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    fmt = _FORMATS[mime]
    if fmt == "JPEG":
        resized.save(out, fmt, quality=JPEG_QUALITY)
    elif fmt == "WEBP":
        resized.save(out, fmt, quality=JPEG_QUALITY)
    else:
        resized.save(out, fmt)
    return out.getvalue()


def convert_for(data: bytes, mime: str, accepted: frozenset[str]) -> tuple[bytes, str]:
    """Convert to an accepted type for export (first frame; png with alpha, else jpeg)."""
    if mime in accepted:
        return data, mime
    with Image.open(io.BytesIO(data)) as image:
        frame = next(ImageSequence.Iterator(image)).copy()
    has_alpha = "A" in frame.getbands() or "transparency" in frame.info
    out = io.BytesIO()
    if has_alpha and "image/png" in accepted:
        frame.convert("RGBA").save(out, "PNG")
        return out.getvalue(), "image/png"
    if "image/jpeg" in accepted:
        frame.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
        return out.getvalue(), "image/jpeg"
    frame.convert("RGBA").save(out, "PNG")
    return out.getvalue(), "image/png"
