"""The VAPID key pair stored in the `settings` table (spec section 4)."""

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from novelhog.repo import Repo

_KEY = "vapid"
_SCALAR_BYTES = 32


@dataclass(frozen=True)
class VapidKeys:
    """URL-safe base64 (no padding) of the 65-byte public point and the 32-byte private scalar."""

    public_key: str
    private_key: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate() -> VapidKeys:
    private = ec.generate_private_key(ec.SECP256R1())
    scalar = private.private_numbers().private_value.to_bytes(_SCALAR_BYTES, "big")
    point = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return VapidKeys(public_key=_b64(point), private_key=_b64(scalar))


def load(repo: Repo) -> VapidKeys | None:
    """Return the stored key pair, or None when there is none."""
    value = repo.settings.get(_KEY)
    if value is None:
        return None
    return VapidKeys(public_key=value["public_key"], private_key=value["private_key"])


def ensure_keys(repo: Repo) -> VapidKeys:
    """Return the stored key pair, creating it first if missing.

    The insert is a no-op when another instance won the race, so the pair is re-read afterwards
    and every caller ends with the one stored pair. A stored pair is never replaced.
    """
    existing = load(repo)
    if existing is not None:
        return existing
    fresh = _generate()
    repo.settings.put_if_absent(
        _KEY, {"public_key": fresh.public_key, "private_key": fresh.private_key}
    )
    stored = load(repo)
    if stored is None:  # pragma: no cover - the row was just written or already present
        msg = "VAPID keys missing after insert"
        raise RuntimeError(msg)
    return stored
