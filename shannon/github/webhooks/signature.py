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

    # compare_digest keeps the check constant time, so a wrong signature cannot be narrowed
    # down byte by byte from response timing. It only accepts ASCII, and this header comes
    # off the network, so anything else is malformed rather than an excuse to raise.
    try:
        matched = hmac.compare_digest(sign(body, secret), header_value)
    except TypeError:
        return SignatureResult.MALFORMED

    return SignatureResult.VALID if matched else SignatureResult.INVALID


def verify_any(body: bytes, secrets: Sequence[str], header_value: str | None) -> SignatureResult:
    """The same check against several secrets, for a deployment moving between them.

    A GitHub App has one webhook secret of its own, and a repository configured by hand before the
    App existed has another. Both have to be accepted while a deployment moves across, or the
    change is a flag day: delete the old webhook a moment too early and deliveries are refused,
    a moment too late and they arrive twice.

    Every secret is checked, and the loop is deliberately not stopped at the first match. The
    comparison inside `verify` is already constant time per secret, but returning early would make
    the NUMBER of comparisons depend on which secret matched, so the time taken would say which
    one it was. That is a weak oracle and it costs nothing to close.

    Empty secrets are dropped rather than checked. An unset secret is not a secret, and signing
    with the empty string would accept anything from anybody who guessed that it was unset. With
    nothing left the answer is MISSING, which the route already fails closed on.

    The failure that comes back is the first one, and that is exact rather than convenient:
    MISSING and MALFORMED are decided by the header alone, so every secret reaches the same
    verdict on them, and INVALID is what is left.
    """
    usable = [secret for secret in secrets if secret]
    if not usable:
        return SignatureResult.MISSING

    results = [verify(body, secret, header_value) for secret in usable]
    return SignatureResult.VALID if SignatureResult.VALID in results else results[0]
