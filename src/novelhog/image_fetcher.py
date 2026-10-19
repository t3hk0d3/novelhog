"""Image fetcher for the HTML to Markdown converter (spec section 8.2)."""

import io

import requests
from PIL import Image, UnidentifiedImageError

from novelhog import images
from novelhog.images import FetchedImage
from novelhog.net import Refused

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
_CHUNK = 16 * 1024


class ImageFetcher:
    """Downloads one image through a guarded session, enforcing size and pixel caps."""

    def __init__(
        self,
        session: requests.Session,
        *,
        max_bytes: int,
        max_pixels: int,
        max_dimension: int,
    ) -> None:
        self._session = session
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._max_dimension = max_dimension
        Image.MAX_IMAGE_PIXELS = max_pixels  # Pillow's own bomb limit, same value (spec 8.2)

    def fetch(self, url: str) -> FetchedImage | None:
        """Return the image, or None when it is unavailable, refused, oversized or not allowed."""
        try:
            data = self._download(url)
            if data is None:
                return None
            mime = images.sniff(data)
            if mime is None:
                return None
            images.check_pixels(data, self._max_pixels)
            return FetchedImage(images.downscale(data, mime, self._max_dimension), mime)
        except (
            requests.RequestException,
            Refused,
            images.TooLarge,
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
        ):
            return None

    def _download(self, url: str) -> bytes | None:
        limit = self._max_bytes + 1
        with self._session.get(url, stream=True, headers={"User-Agent": USER_AGENT}) as response:
            if not response.ok:
                return None
            buffer = io.BytesIO()
            for chunk in response.iter_content(_CHUNK):
                buffer.write(chunk[: limit - buffer.tell()])
                if buffer.tell() >= limit:
                    return None
            return buffer.getvalue()
