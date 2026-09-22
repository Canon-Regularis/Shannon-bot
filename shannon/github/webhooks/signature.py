from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
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

    # compare_digest keeps the check constant time, so a wrong signature cannot be narrowed down
    # byte by byte from response timing. It accepts ASCII only, and this header comes off the
    # network, so anything else is malformed rather than a raise.
    try:
        matched = hmac.compare_digest(sign(body, secret), header_value)
    except TypeError:
        return SignatureResult.MALFORMED

    return SignatureResult.VALID if matched else SignatureResult.INVALID


def verify_any(body: bytes, secrets: Sequence[str], header_value: str | None) -> SignatureResult:
    """The same check against several secrets, for a deployment moving between them.

    A GitHub App's own webhook secret and a hand-configured repository's are both accepted while
    a deployment moves across, or the change is a flag day. Every secret is checked with no early
    return, so the number of comparisons cannot say which secret matched. Empty secrets are
    dropped: signing with the empty string would accept anything from anybody who guessed it was
    unset, and with none left the answer is MISSING, which the route fails closed on. The first
    failure is returned, which is exact because MISSING and MALFORMED depend on the header alone.
    """
    usable = [secret for secret in secrets if secret]
    if not usable:
        return SignatureResult.MISSING

    results = [verify(body, secret, header_value) for secret in usable]
    return SignatureResult.VALID if SignatureResult.VALID in results else results[0]
