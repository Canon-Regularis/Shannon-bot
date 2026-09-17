"""Signing the token that proves this process is the GitHub App it claims to be.

A GitHub App has no standing credential of its own. It holds a private key, signs a short-lived
JWT with it, and trades that JWT for an installation token scoped to one account. This module is
the first half: it turns a key and a clock into the JWT, and knows nothing about HTTP.

Pure and synchronous on purpose. Signing is the one step here with a wrong answer that cannot be
retried out of, so it is worth being able to test against a literal without a network or a clock.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shannon.github.errors import GitHubAuthError

# GitHub requires RS256 and rejects anything else, so it is written into the header rather than
# offered as a choice. Nothing here reads a JWT, only writes them, which is why this module does
# not depend on a JWT library: the whole value of one is verifying somebody else's token safely.
_HEADER = {"alg": "RS256", "typ": "JWT"}

# Backdated, because GitHub compares `iat` against its own clock and rejects one in the future.
# A server a few seconds fast would otherwise fail every mint, intermittently, in a way that
# looks like a bad key. GitHub's own documentation recommends exactly this.
CLOCK_DRIFT = timedelta(seconds=60)

# GitHub refuses a JWT claiming more than ten minutes. Nine leaves room for the drift above plus
# the round trip, and this token is spent immediately on one call, so a longer life buys nothing.
LIFETIME = timedelta(minutes=9)


def app_jwt(*, client_id: str, private_key_pem: str, now: datetime) -> str:
    """The App's own bearer token, or the empty string when no App is configured.

    Empty rather than an exception, because "no App configured" is a running state this project
    already models: `_headers` sends no `Authorization` at all for an empty token, and the
    deployment answers public endpoints and fails private ones with a message saying so. An
    exception here would turn a configuration gap into a crash on the first command.

    A key that is present and unusable is the opposite, and raises. Somebody set it, so somebody
    wants to know it is wrong rather than watch every repository quietly report as missing.

    `now` is passed in rather than read, so a test can sign against a literal and assert the
    claims exactly. Every caller has a clock; none of them has a reason to disagree about it.
    """
    if not client_id or not private_key_pem:
        return ""

    issued = now - CLOCK_DRIFT
    claims = {
        "iat": int(issued.timestamp()),
        "exp": int((issued + LIFETIME).timestamp()),
        # The client id rather than the numeric App id. GitHub accepts both and now recommends
        # this one, and it is the same value the OAuth flow needs, so the deployment configures
        # one identifier instead of two that must agree.
        "iss": client_id,
    }

    signing_input = f"{_segment(_HEADER)}.{_segment(claims)}".encode("ascii")
    signature = _sign(signing_input, private_key_pem)
    return f"{signing_input.decode('ascii')}.{_b64(signature)}"


def _sign(signing_input: bytes, private_key_pem: str) -> bytes:
    """RS256 over the signing input, with a usable message for every way the key can be wrong.

    GitHub hands out a PKCS#1 PEM and some tooling converts it to PKCS#8 on the way through;
    `load_pem_private_key` reads either, so neither is worth branching on.

    The errors are caught by type rather than swallowed wholesale. What reaches here is a value
    somebody pasted into an environment variable, so the ways it goes wrong are: not a key,
    truncated, encrypted with a passphrase this cannot supply, or an algorithm that is not RSA.
    All four are the same answer to an operator, and all four are permanent.
    """
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise GitHubAuthError(
            "The GitHub App private key could not be read. Check "
            "SHANNON_GITHUB_APP_PRIVATE_KEY holds the whole .pem GitHub issued."
        ) from exc

    # An Ed25519 or EC key loads perfectly and then cannot sign RS256, so the check is on the
    # kind of key rather than on the load succeeding. GitHub only ever issues RSA.
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAuthError(
            "The GitHub App private key is not an RSA key, and GitHub signs App tokens with "
            "RS256. Download the .pem from the App's settings page again."
        )

    return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


def _segment(payload: dict[str, object]) -> str:
    """One JWT segment: compact JSON, base64url, no padding.

    Separators are given explicitly because `json.dumps` puts a space after each one by default,
    and those spaces are inside the signed bytes. It would still verify, since the signature
    covers whatever was sent, but it makes the token longer than it needs to be for no reason.
    """
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64(encoded)


def _b64(raw: bytes) -> str:
    """base64url without padding, which is what the JWT specification asks for."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
