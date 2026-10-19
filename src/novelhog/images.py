"""Image handling (spec sections 8.2 and 8.4)."""

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, ImageSequence

JPEG_QUALITY = 90

_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}


# Images between 1x and 2x Pillow's limit only warn; check_pixels rejects them explicitly.
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
# Corrupt EXIF only produces a UserWarning from Pillow's TIFF reader; keep it out of the logs.
warnings.filterwarnings("ignore", category=UserWarning, module=r"PIL\.TiffImagePlugin")


def configure_pixel_limit(max_pixels: int) -> None:
    """Set Pillow's decompression-bomb limit to ``max_pixels`` (idempotent; call at startup)."""
    Image.MAX_IMAGE_PIXELS = max_pixels


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
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Image.DecompressionBombError as exc:
        raise TooLarge(str(exc)) from exc  # noqa: TRY003
    if width * height > max_pixels:
        raise TooLarge(f"{width}x{height} exceeds {max_pixels} pixels")  # noqa: TRY003


def _is_animated(image: Image.Image) -> bool:
    return bool(getattr(image, "is_animated", False))


def _webp_chunks(data: bytes) -> list[str]:
    """Four-character codes of the top-level chunks in a RIFF/WebP file."""
    codes: list[str] = []
    pos = 12
    while pos + 8 <= len(data):
        codes.append(data[pos : pos + 4].decode("latin-1"))
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        pos += 8 + size + (size & 1)
    return codes


def _is_lossless_webp(data: bytes) -> bool:
    return "VP8L" in _webp_chunks(data)


def downscale(data: bytes, mime: str, max_dimension: int) -> bytes:
    """Shrink so the longest side is at most ``max_dimension``, in the original format.

    EXIF orientation is applied first and the ICC profile is kept; lossless WebP stays lossless.
    """
    if max_dimension <= 0:
        return data
    with Image.open(io.BytesIO(data)) as image:
        if _is_animated(image) or max(image.size) <= max_dimension:
            return data
        image.load()
        resized = ImageOps.exif_transpose(image)
    resized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    fmt = _FORMATS[mime]
    options: dict[str, object] = {}
    icc = resized.info.get("icc_profile")
    if icc:
        options["icc_profile"] = icc
    exif = resized.getexif()
    if exif:
        options["exif"] = exif.tobytes()
    if fmt == "JPEG":
        options["quality"] = JPEG_QUALITY
    elif fmt == "WEBP":
        if _is_lossless_webp(data):
            options["lossless"] = True
        else:
            options["quality"] = JPEG_QUALITY
    out = io.BytesIO()
    resized.save(out, fmt, **options)
    return out.getvalue()


def convert_for(data: bytes, mime: str, accepted: frozenset[str]) -> tuple[bytes, str]:
    """Convert to an accepted type for export, using the first frame.

    GIF and anything with alpha become PNG; everything else becomes JPEG (quality 90).
    """
    if mime in accepted:
        return data, mime
    with Image.open(io.BytesIO(data)) as image:
        frame = next(ImageSequence.Iterator(image)).copy()
    has_alpha = "A" in frame.getbands() or "transparency" in frame.info
    out = io.BytesIO()
    if (has_alpha or mime == "image/gif") and "image/png" in accepted:
        frame.convert("RGBA").save(out, "PNG")
        return out.getvalue(), "image/png"
    if "image/jpeg" in accepted:
        frame.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
        return out.getvalue(), "image/jpeg"
    frame.convert("RGBA").save(out, "PNG")
    return out.getvalue(), "image/png"
