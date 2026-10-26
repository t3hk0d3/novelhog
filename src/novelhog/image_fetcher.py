"""Image fetcher for covers and the HTML to Markdown converter (spec section 8.2).

A direct request that meets a Cloudflare challenge has FlareSolverr load the challenged URL and
is sent once more with the cookies and user agent of the clearance it earned. The clearance is
cached (`FlareSolverr.clearances`), so the site's next images and pages go straight through with
it; one the site challenges anyway is forgotten.
"""

import io
import time
from dataclasses import dataclass

import requests
from urllib3.exceptions import HTTPError

from novelhog import images, log
from novelhog.clearance import Clearance
from novelhog.flaresolverr import FlareSolverr, FlareSolverrFailed, FlareSolverrUnavailable
from novelhog.images import FetchedImage
from novelhog.net import Refused

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
_CHUNK = 16 * 1024
DOWNLOAD_DEADLINE = 60.0  # seconds for one whole download


_CHALLENGE_STATUSES = (403, 503)


@dataclass(frozen=True)
class _Challenged:
    """A Cloudflare challenge answered the request; `url` is the challenged URL after redirects."""

    url: str


def _is_challenge(response: requests.Response) -> bool:
    return (
        response.status_code in _CHALLENGE_STATUSES
        and response.headers.get("cf-mitigated", "").lower() == "challenge"
    )


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
        flaresolverr: FlareSolverr | None = None,
    ) -> None:
        self._session = session
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._max_dimension = max_dimension
        self._deadline = deadline
        self._flaresolverr = flaresolverr

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
        """Download directly; on a Cloudflare challenge solve it and try once more."""
        solver = self._flaresolverr
        clearance = None if solver is None else solver.clearances.for_url(url)
        result = self._attempt(url, clearance)
        if not isinstance(result, _Challenged):
            return result
        if solver is None:
            return None
        if clearance is not None:
            solver.clearances.forget(clearance)
        clearance = self._solve(solver, result.url)
        if clearance is None:
            return None
        result = self._attempt(url, clearance)
        if isinstance(result, _Challenged):
            solver.clearances.forget(clearance)
            log.get().warning("image_clearance_refused", domain=clearance.domain, url=url)
            return None
        return result

    @staticmethod
    def _solve(solver: FlareSolverr, url: str) -> Clearance | None:
        """The clearance FlareSolverr earns loading `url`; None (logged) when it earns none."""
        try:
            clearance = solver.get(url).clearance
        except (FlareSolverrUnavailable, FlareSolverrFailed) as error:
            log.get().warning("image_clearance_failed", url=url, error=str(error))
            return None
        if clearance is None:
            log.get().warning("image_clearance_failed", url=url, error="no cf_clearance cookie")
        return clearance

    def _attempt(self, url: str, clearance: Clearance | None) -> bytes | _Challenged | None:
        """One direct download, sending `clearance` when given; the deadline is per attempt."""
        limit = self._max_bytes + 1
        expires = time.monotonic() + self._deadline
        user_agent = USER_AGENT if clearance is None else clearance.user_agent
        cookies = None if clearance is None else clearance.jar()
        with self._session.get(
            url, stream=True, headers={"User-Agent": user_agent}, cookies=cookies
        ) as response:
            if not response.ok:
                return _Challenged(response.url) if _is_challenge(response) else None
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
