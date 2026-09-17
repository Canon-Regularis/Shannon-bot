from __future__ import annotations

import hashlib
import hmac

import pytest

from shannon.github.webhooks import signature
from shannon.github.webhooks.signature import SignatureResult, sign, verify, verify_any

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


class TestVerifyingAgainstSeveralSecrets:
    """A deployment moving from a hand-configured repository webhook to a GitHub App's own.

    Both secrets have to be accepted for the window in between, or the change is a flag day:
    delete the old webhook a moment too early and deliveries are refused, a moment too late and
    every one of them arrives twice.
    """

    def test_the_first_secret_verifies(self) -> None:
        body = b'{"action":"opened"}'

        assert (
            verify_any(body, ("app-secret", "legacy"), sign(body, "app-secret"))
            is SignatureResult.VALID
        )

    def test_the_second_one_does_too(self) -> None:
        body = b'{"action":"opened"}'

        assert (
            verify_any(body, ("app-secret", "legacy"), sign(body, "legacy"))
            is SignatureResult.VALID
        )

    def test_a_third_party_secret_does_not(self) -> None:
        body = b'{"action":"opened"}'

        assert (
            verify_any(body, ("app-secret", "legacy"), sign(body, "guessed"))
            is SignatureResult.INVALID
        )

    def test_one_secret_still_works(self) -> None:
        body = b'{"action":"opened"}'

        assert verify_any(body, ("only",), sign(body, "only")) is SignatureResult.VALID

    def test_an_empty_secret_is_dropped_rather_than_checked(self) -> None:
        """An unset secret is not a secret. Signing with the empty string would let anybody who
        guessed it was unset sign their own deliveries."""
        body = b'{"action":"opened"}'

        assert verify_any(body, ("", "real"), sign(body, "real")) is SignatureResult.VALID
        assert verify_any(body, ("", "real"), sign(body, "")) is SignatureResult.INVALID

    def test_no_usable_secret_at_all_fails_closed(self) -> None:
        """MISSING is what the route turns into its existing 500. An unconfigured deployment must
        refuse deliveries rather than wave them through."""
        body = b'{"action":"opened"}'

        assert verify_any(body, ("", ""), sign(body, "anything")) is SignatureResult.MISSING
        assert verify_any(body, (), sign(body, "anything")) is SignatureResult.MISSING

    def test_a_header_that_is_not_there_reads_the_same_as_before(self) -> None:
        assert verify_any(b"{}", ("a", "b"), None) is SignatureResult.MISSING

    def test_a_malformed_header_reads_the_same_as_before(self) -> None:
        """Decided by the header alone, so every secret reaches the same verdict and reporting the
        first is exact rather than convenient."""
        assert verify_any(b"{}", ("a", "b"), "md5=abc") is SignatureResult.MALFORMED

    def test_the_body_still_has_to_match(self) -> None:
        """The signature covers the body. A valid signature for a different body is somebody
        replaying one delivery's header onto another's content."""
        signed_for_something_else = sign(b'{"action":"closed"}', "app-secret")

        assert (
            verify_any(b'{"action":"opened"}', ("app-secret",), signed_for_something_else)
            is SignatureResult.INVALID
        )

    def test_every_secret_is_checked_even_once_one_has_matched(self) -> None:
        """Not an optimisation left undone: stopping early would make the number of comparisons
        depend on which secret matched, so the time taken would say which one it was. A weak
        oracle, and free to close.

        Counted through `sign`, which is what each comparison costs.
        """
        body = b'{"action":"opened"}'
        counted: list[str] = []
        real_sign = signature.sign

        def counting(payload: bytes, secret: str) -> str:
            counted.append(secret)
            return real_sign(payload, secret)

        signature.sign = counting
        try:
            verify_any(body, ("first", "second", "third"), real_sign(body, "first"))
        finally:
            signature.sign = real_sign

        assert counted == ["first", "second", "third"], "it stopped as soon as one matched"
