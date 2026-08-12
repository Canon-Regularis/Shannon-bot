from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum

SIGNATURE_PREFIX = "sha256="


class SignatureResult(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    MALFORMED = "MALFORMED"
    INVALID = "INVALID"


def sign(body: bytes, secret: str) -> str:
    """Produce the header value GitHub would send for this body and secret."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify(body: bytes, secret: str, header_value: str | None) -> SignatureResult:
    if not header_value:
        return SignatureResult.MISSING
    if not header_value.startswith(SIGNATURE_PREFIX):
        return SignatureResult.MALFORMED

    # compare_digest keeps the check constant time, so a wrong signature cannot be narrowed
    # down byte by byte from response timing. It only accepts ASCII, and this header comes
    # off the network, so anything else is malformed rather than an excuse to raise.
    try:
        matched = hmac.compare_digest(sign(body, secret), header_value)
    except TypeError:
        return SignatureResult.MALFORMED

    return SignatureResult.VALID if matched else SignatureResult.INVALID
