"""Worker runtime helpers: startup, the claim heartbeat and the loop's error backoff (spec 6.1)."""

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Self

from novelhog import images, log
from novelhog.config import Settings

_BACKOFF_START_S = 5.0


def configure_runtime(settings: Settings) -> None:
    """Process-wide worker setup; the loop (Task 25) calls it once at startup."""
    images.configure_pixel_limit(settings.max_image_pixels)


class Heartbeat:
    """Context manager that calls `renew` every `interval_s` seconds on a daemon thread.

    `lost` turns True once `renew` returns False; renewing then stops. A `renew` that raises
    (for example a busy database) is logged and retried at the next tick. Leaving the block
    stops the thread and waits for it.
    """

    def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
        self._renew = renew
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self.lost = False

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                renewed = self._renew()
            except Exception as error:  # noqa: BLE001 - a failed renewal must not kill the thread
                log.get().warning(
                    "heartbeat_renew_failed", error=f"{type(error).__name__}: {error}"
                )
                continue
            if not renewed:
                self.lost = True
                return


class Backoff:
    """Delays 5 s, 10 s, 20 s, ... capped at `cap_s`; `reset` starts again at 5 s."""

    def __init__(self, cap_s: float) -> None:
        self._cap_s = cap_s
        self._next_s = _BACKOFF_START_S

    def next(self) -> float:
        """Return the next delay and double the one after it."""
        delay = min(self._next_s, self._cap_s)
        self._next_s = min(self._next_s * 2, self._cap_s)
        return delay

    def reset(self) -> None:
        """Start again at 5 s."""
        self._next_s = _BACKOFF_START_S
