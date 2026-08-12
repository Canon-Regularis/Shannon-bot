from __future__ import annotations

import hashlib
import hmac

import pytest

from shannon.github.webhooks.signature import SignatureResult, sign, verify

SECRET = "it's a secret to everybody"
BODY = b'{"action": "opened"}'


def test_sign_matches_the_github_recipe() -> None:
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()

    assert sign(BODY, SECRET) == f"sha256={expected}"


def test_valid_signature_passes() -> None:
    assert verify(BODY, SECRET, sign(BODY, SECRET)) is SignatureResult.VALID


@pytest.mark.parametrize("header", [None, ""])
def test_missing_signature_fails(header: str | None) -> None:
    assert verify(BODY, SECRET, header) is SignatureResult.MISSING


@pytest.mark.parametrize(
    "header",
    [
        "sha1=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "deadbeef",
        "sha256",
    ],
)
def test_malformed_signature_fails(header: str) -> None:
    assert verify(BODY, SECRET, header) is SignatureResult.MALFORMED


def test_signature_from_a_different_secret_fails() -> None:
    assert verify(BODY, SECRET, sign(BODY, "wrong-secret")) is SignatureResult.INVALID


def test_signature_for_a_different_body_fails() -> None:
    assert verify(BODY, SECRET, sign(b'{"action": "closed"}', SECRET)) is SignatureResult.INVALID


def test_a_single_flipped_byte_fails() -> None:
    tampered = BODY.replace(b"opened", b"openeD")

    assert verify(tampered, SECRET, sign(BODY, SECRET)) is SignatureResult.INVALID


def test_a_non_ascii_signature_header_is_malformed_rather_than_a_crash() -> None:
    """The header comes off the network, and compare_digest only accepts ASCII."""
    assert verify(b"{}", "secret", "sha256=\u00e9" + "a" * 63) is SignatureResult.MALFORMED


def test_a_header_of_the_right_shape_but_wrong_digest_is_invalid() -> None:
    assert verify(b"{}", "secret", "sha256=" + "a" * 64) is SignatureResult.INVALID
