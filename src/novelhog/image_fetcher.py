"""Image fetcher for the HTML to Markdown converter (spec section 8.2)."""

import io
import time

import requests
from urllib3.exceptions import HTTPError

from novelhog import images
from novelhog.images import FetchedImage
from novelhog.net import Refused

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
_CHUNK = 16 * 1024
DOWNLOAD_DEADLINE = 60.0  # seconds for one whole download


class ImageFetcher:
    """Downloads one image through a guarded session, enforcing size and pixel caps."""

    def __init__(
        self,
        session: requests.Session,
        *,
        max_bytes: int,
        max_pixels: int,
        max_dimension: int,
        deadline: float = DOWNLOAD_DEADLINE,
    ) -> None:
        self._session = session
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._max_dimension = max_dimension
        self._deadline = deadline

    def fetch(self, url: str) -> FetchedImage | None:
        """Return the image, or None when it is unavailable, refused, oversized or not allowed."""
        try:
            data = self._download(url)
        except requests.RequestException, HTTPError, Refused, OSError:
            return None
        if data is None:
            return None
        try:
            mime = images.sniff(data)
            if mime is None:
                return None
            images.check_pixels(data, self._max_pixels)
            return FetchedImage(images.downscale(data, mime, self._max_dimension), mime)
        except Exception:  # noqa: BLE001 - Pillow raises many types on corrupt input
            return None

    def _download(self, url: str) -> bytes | None:
        limit = self._max_bytes + 1
        expires = time.monotonic() + self._deadline
        with self._session.get(url, stream=True, headers={"User-Agent": USER_AGENT}) as response:
            if not response.ok:
                return None
            buffer = io.BytesIO()
            while True:
                if time.monotonic() > expires:
                    return None
                chunk = response.raw.read1(_CHUNK, decode_content=True)
                if not chunk:
                    return buffer.getvalue()
                buffer.write(chunk[: limit - buffer.tell()])
                if buffer.tell() >= limit:
                    return None
