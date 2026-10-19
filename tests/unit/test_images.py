"""Tests for the image helpers."""

import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageCms

from novelhog import images
from tests.fixtures.images.helpers import make_animated, make_image

EXPORT = frozenset({"image/jpeg", "image/png", "image/gif"})


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


class TestSniff__Types:
    @pytest.mark.parametrize(
        ("fmt", "mime"),
        [
            ("PNG", "image/png"),
            ("JPEG", "image/jpeg"),
            ("GIF", "image/gif"),
            ("WEBP", "image/webp"),
        ],
    )
    def test_sniff__recognises_allowed_types(self, fmt: str, mime: str) -> None:
        assert images.sniff(make_image(fmt)) == mime

    @pytest.mark.parametrize(
        "data", [b"<svg xmlns='x'/>", b"<html>", b"", b"RIFF1234WAVE", b"random"]
    )
    def test_sniff__none_for_other_data(self, data: bytes) -> None:
        assert images.sniff(data) is None


class TestCheckPixels__Header:
    def test_check_pixels__raises_without_decoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = make_image("PNG", (100, 100))

        def no_load(self: Image.Image) -> None:
            raise AssertionError("decoded")

        monkeypatch.setattr(Image.Image, "load", no_load)
        with pytest.raises(images.TooLarge):
            images.check_pixels(data, 9999)

    def test_check_pixels__accepts_at_limit(self) -> None:
        images.check_pixels(make_image("PNG", (100, 100)), 10000)


class TestDownscale__Oversized:
    def test_downscale__keeps_aspect_and_format(self) -> None:
        out = images.downscale(make_image("PNG", (400, 200)), "image/png", 100)
        assert size_of(out) == (100, 50)
        assert images.sniff(out) == "image/png"

    def test_downscale__jpeg_quality_90(self) -> None:
        out = images.downscale(make_image("JPEG", (400, 200)), "image/jpeg", 100)
        with Image.open(io.BytesIO(out)) as img:
            assert img.format == "JPEG"
            assert img.size == (100, 50)
            q = img.quantization[0]  # type: ignore[attr-defined]
        with Image.open(io.BytesIO(make_image("JPEG", (10, 10), quality=90))) as ref:
            assert q == ref.quantization[0]  # type: ignore[attr-defined]


class TestDownscale__Unchanged:
    def test_downscale__within_limit_unchanged(self) -> None:
        data = make_image("PNG", (50, 50))
        assert images.downscale(data, "image/png", 100) == data

    def test_downscale__animated_gif_unchanged(self) -> None:
        data = make_animated("GIF")
        assert images.downscale(data, "image/gif", 4) == data

    def test_downscale__animated_webp_unchanged(self) -> None:
        data = make_animated("WEBP")
        assert images.downscale(data, "image/webp", 4) == data

    def test_downscale__zero_disables(self) -> None:
        data = make_image("PNG", (400, 200))
        assert images.downscale(data, "image/png", 0) == data


class TestConvertFor__Accepted:
    def test_convert_for__unchanged(self) -> None:
        data = make_image("PNG")
        assert images.convert_for(data, "image/png", EXPORT) == (data, "image/png")


class TestConvertFor__Webp:
    def test_convert_for__alpha_webp_to_png(self) -> None:
        data, mime = images.convert_for(make_image("WEBP", mode="RGBA"), "image/webp", EXPORT)
        assert mime == "image/png"
        assert images.sniff(data) == "image/png"

    def test_convert_for__opaque_webp_to_jpeg(self) -> None:
        data, mime = images.convert_for(make_image("WEBP"), "image/webp", EXPORT)
        assert mime == "image/jpeg"
        assert images.sniff(data) == "image/jpeg"

    def test_convert_for__animated_webp_first_frame(self) -> None:
        data, mime = images.convert_for(make_animated("WEBP"), "image/webp", EXPORT)
        with Image.open(io.BytesIO(data)) as img:
            r, _, b = img.convert("RGB").getpixel((0, 0))  # type: ignore[misc]
        assert mime == "image/jpeg"
        assert r > b

    def test_convert_for__opaque_gif_to_png_first_frame(self) -> None:
        data, mime = images.convert_for(
            make_animated("GIF"), "image/gif", frozenset({"image/jpeg", "image/png"})
        )
        assert mime == "image/png"
        with Image.open(io.BytesIO(data)) as img:
            r, _, b = img.convert("RGB").getpixel((0, 0))  # type: ignore[misc]
        assert r > b


class TestDownscale__Metadata:
    def test_downscale__applies_exif_orientation(self) -> None:
        src = Image.new("RGB", (40, 20), (200, 10, 10))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise on display
        out = io.BytesIO()
        src.save(out, "JPEG", exif=exif)
        result = images.downscale(out.getvalue(), "image/jpeg", 10)
        with Image.open(io.BytesIO(result)) as img:
            assert img.size == (5, 10)
            assert img.getexif().get(0x0112, 1) == 1

    def test_downscale__keeps_icc_profile(self) -> None:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        data = make_image("JPEG", (40, 20), icc_profile=profile)
        result = images.downscale(data, "image/jpeg", 10)
        with Image.open(io.BytesIO(result)) as img:
            assert img.info.get("icc_profile") == profile

    def test_downscale__keeps_lossless_webp_lossless(self) -> None:
        data = make_image("WEBP", (40, 20), lossless=True)
        assert b"VP8L" in data[:32]
        result = images.downscale(data, "image/webp", 10)
        assert b"VP8L" in result[:32]

    def test_downscale__keeps_lossy_webp_lossy(self) -> None:
        result = images.downscale(make_image("WEBP", (40, 20)), "image/webp", 10)
        assert b"VP8 " in result[:32]


class TestDownscale__LosslessWebpLayouts:
    @pytest.mark.parametrize("extra", ["icc", "exif"])
    def test_downscale__extended_lossless_webp_stays_lossless(self, extra: str) -> None:
        options: dict[str, Any] = {}
        if extra == "icc":
            options["icc_profile"] = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes()
        else:
            exif = Image.Exif()
            exif[0x010F] = "Camera"
            options["exif"] = exif
        data = make_image("WEBP", (40, 20), lossless=True, **options)
        assert images._webp_chunks(data)[0] == "VP8X"  # extended layout
        result = images.downscale(data, "image/webp", 10)
        assert "VP8L" in images._webp_chunks(result)
        assert "VP8 " not in images._webp_chunks(result)


class TestDownscale__CorruptExif:
    def test_downscale__corrupt_exif_emits_no_warning(self, tmp_path: Path) -> None:
        # pytest resets the warning filters per test, so run a fresh interpreter instead.
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[0x010F] = "CameraCameraCamera"
        out = io.BytesIO()
        Image.new("RGB", (40, 20)).save(out, "JPEG", exif=exif)
        data = bytearray(out.getvalue())
        data[data.index(b"Exif\0\0") + 11] = 0xFF
        sample = tmp_path / "corrupt_exif.jpg"
        sample.write_bytes(bytes(data))
        script = (
            "import sys; from novelhog import images; "
            "images.downscale(open(sys.argv[1], 'rb').read(), 'image/jpeg', 10)"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", script, str(sample)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode, result.stderr) == (0, "")


class TestConfigurePixelLimit__Idempotent:
    def test_configure_pixel_limit__sets_pillow_limit_and_is_idempotent(self) -> None:
        images.configure_pixel_limit(1234)
        images.configure_pixel_limit(1234)
        assert Image.MAX_IMAGE_PIXELS == 1234
