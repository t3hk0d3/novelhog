"""Text sanitising (spec section 8.1)."""

import re

_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def strip_xml_invalid(value: str) -> str:
    """Remove characters that are not allowed in XML 1.0, including lone surrogates."""
    return _XML_INVALID.sub("", value)
