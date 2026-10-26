"""Identifier generation and validation (spec section 3)."""

import re
import secrets

PREFIXES: frozenset[str] = frozenset({"NV", "CH", "EX", "SB"})

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_BODY_LENGTH = 12
_BODY = re.compile(rf"[a-z0-9]{{{_BODY_LENGTH}}}")


def new_id(prefix: str) -> str:
    """Return a fresh identifier: the prefix followed by 12 random `[a-z0-9]` characters."""
    if prefix not in PREFIXES:
        msg = f"unknown id prefix: {prefix!r}"
        raise ValueError(msg)
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LENGTH))


def is_valid(value: str, prefix: str) -> bool:
    """Report whether `value` is a well-formed identifier with the given prefix."""
    if prefix not in PREFIXES or not value.startswith(prefix):
        return False
    return _BODY.fullmatch(value, len(prefix)) is not None
