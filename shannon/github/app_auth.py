"""Signing the token that proves this process is the GitHub App it claims to be.

A GitHub App has no standing credential of its own: it signs a short-lived JWT with its private
key and trades that for an installation token scoped to one account. This module is the first
half, and knows nothing about HTTP.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timedelta

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from shannon.github.errors import GitHubAuthError

# GitHub requires RS256 and rejects anything else, so it is written into the header rather than
# offered as a choice. No JWT library, because nothing here reads a token, only writes them.
_HEADER = {"alg": "RS256", "typ": "JWT"}

# Backdated, because GitHub compares `iat` against its own clock and rejects one in the future:
# a server seconds fast would otherwise fail every mint in a way that looks like a bad key.
CLOCK_DRIFT = timedelta(seconds=60)

# GitHub refuses a JWT claiming more than ten minutes. Nine leaves room for the drift above plus
# the round trip, and this token is spent immediately on one call.
LIFETIME = timedelta(minutes=9)


def app_jwt(*, client_id: str, private_key_pem: str, now: datetime) -> str:
    """The App's own bearer token, or the empty string when no App is configured.

    Empty rather than an exception, because no App configured is a running state: `_headers`
    sends no `Authorization` for an empty token. A key present but unusable raises instead.
    """
    if not client_id or not private_key_pem:
        return ""

    issued = now - CLOCK_DRIFT
    claims = {
        "iat": int(issued.timestamp()),
        "exp": int((issued + LIFETIME).timestamp()),
        # The client id rather than the numeric App id: GitHub accepts both, and this is the same
        # value the OAuth flow needs, so the deployment configures one identifier.
        "iss": client_id,
    }

    signing_input = f"{_segment(_HEADER)}.{_segment(claims)}".encode("ascii")
    signature = _sign(signing_input, private_key_pem)
    return f"{signing_input.decode('ascii')}.{_b64(signature)}"


def _sign(signing_input: bytes, private_key_pem: str) -> bytes:
    """RS256 over the signing input, with a usable message for every way the key can be wrong.

    `load_pem_private_key` reads both the PKCS#1 PEM GitHub issues and the PKCS#8 some tooling
    converts it to, so neither is worth branching on. Every way a pasted key fails is permanent.
    """
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise GitHubAuthError(
            "The GitHub App private key could not be read. Check "
            "SHANNON_GITHUB_APP_PRIVATE_KEY holds the whole .pem GitHub issued."
        ) from exc

    # An Ed25519 or EC key loads perfectly and then cannot sign RS256, so the check is on the
    # kind of key rather than on the load succeeding.
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAuthError(
            "The GitHub App private key is not an RSA key, and GitHub signs App tokens with "
            "RS256. Download the .pem from the App's settings page again."
        )

    return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


def _segment(payload: Mapping[str, object]) -> str:
    """One JWT segment: compact JSON, base64url, no padding.

    Separators are given explicitly because `json.dumps` puts a space after each one by default,
    and those spaces would sit inside the signed bytes.
    """
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64(encoded)


def _b64(raw: bytes) -> str:
    """base64url without padding, which is what the JWT specification asks for."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
