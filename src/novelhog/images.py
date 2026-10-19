"""Image handling (spec section 8.2)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FetchedImage:
    """A downloaded image: its bytes and the MIME type detected from its magic bytes."""

    data: bytes
    mime: str
