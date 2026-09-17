"""The JWT that proves this process is the GitHub App, checked against what GitHub will accept.

Issue #98. Everything else in the private-repository work depends on this one string being right,
and being wrong about it fails in the least helpful way available: GitHub answers 401 to the token
mint, every repository then reads as missing, and the message an operator sees is about a
repository rather than about a key.

So the claims are asserted against literals rather than against the constants that produced them,
and the signature is verified with the public half of the key that signed it. A test comparing
`exp - iat` to `LIFETIME` would pass with the lifetime set to a year.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from shannon.github.app_auth import CLOCK_DRIFT, LIFETIME, app_jwt
from shannon.github.errors import GitHubAuthError

pytestmark = pytest.mark.unit

# Generated once. A 2048-bit key costs a noticeable fraction of a second and every test here wants
# the same one, so the alternative is paying for it eight times to learn nothing.
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
CLIENT_ID = "Iv23liAbCdEfGhIjKlMn"


def pem_of(key: object, *, password: bytes | None = None) -> str:
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password is not None
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode("ascii")


PEM = pem_of(KEY)


def parts(token: str) -> tuple[dict, dict, bytes]:
    """The three segments, decoded. Padding is put back because a JWT strips it."""
    header, claims, signature = token.split(".")

    def decode(segment: str) -> bytes:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))

    return json.loads(decode(header)), json.loads(decode(claims)), decode(signature)


def signed(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "client_id": CLIENT_ID,
        "private_key_pem": PEM,
        "now": NOW,
    }
    arguments.update(overrides)
    return app_jwt(**arguments)


class TestWhatGitHubChecks:
    def test_the_algorithm_is_the_only_one_github_accepts(self) -> None:
        """RS256 against a literal. GitHub rejects every other algorithm outright, including the
        `none` that the JWT specification's worst mistake allows."""
        header, _, _ = parts(signed())

        assert header == {"alg": "RS256", "typ": "JWT"}

    def test_the_issuer_is_the_client_id(self) -> None:
        _, claims, _ = parts(signed())

        assert claims["iss"] == CLIENT_ID

    def test_it_is_issued_a_minute_in_the_past(self) -> None:
        """Against 60 rather than against `CLOCK_DRIFT`. GitHub compares `iat` to its own clock
        and refuses one in the future, so a server running a few seconds fast fails every mint,
        intermittently, and it looks exactly like a bad key."""
        _, claims, _ = parts(signed())

        assert int(NOW.timestamp()) - claims["iat"] == 60

    def test_it_expires_inside_the_ten_minutes_github_allows(self) -> None:
        """The cap is GitHub's and the margin is ours. Asserted as a wall-clock window from `now`
        rather than as a difference between the two claims, because the backdating spends part of
        the allowance: a lifetime raised to ten minutes would still read as ten minutes between
        the claims and be refused for being eleven from now."""
        _, claims, _ = parts(signed())

        ahead = claims["exp"] - int(NOW.timestamp())
        assert 0 < ahead < 600

    def test_the_signature_verifies_against_the_key_that_made_it(self) -> None:
        """The whole point, and the one thing no amount of reading the claims can tell you."""
        token = signed()
        _, _, signature = parts(token)
        signing_input = ".".join(token.split(".")[:2]).encode("ascii")

        KEY.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())

    def test_a_different_key_does_not_verify(self) -> None:
        """Otherwise the test above passes against a signature of anything at all."""
        token = signed()
        _, _, signature = parts(token)
        signing_input = ".".join(token.split(".")[:2]).encode("ascii")
        stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        with pytest.raises(InvalidSignature):
            stranger.public_key().verify(
                signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
            )

    def test_the_segments_carry_no_padding(self) -> None:
        """A JWT is base64url with the `=` stripped. GitHub is not lenient about it."""
        assert "=" not in signed()

    def test_a_later_clock_makes_a_later_token(self) -> None:
        """`now` is a parameter rather than a read, and this is what that buys: the claims move
        when the clock does, so nothing has quietly pinned them to import time."""
        _, earlier, _ = parts(signed())
        _, later, _ = parts(signed(now=NOW + timedelta(hours=1)))

        assert later["iat"] - earlier["iat"] == 3600


class TestADeploymentWithNoApp:
    """Empty rather than an exception, because it is a state this project runs in.

    An unset credential already means "do without" everywhere else here: an empty Discord token
    runs the API with no gateway, and an empty GitHub token sends no `Authorization` at all. A
    crash on the first command would be a worse answer than a reply saying what is not set.
    """

    def test_no_client_id_is_no_token(self) -> None:
        assert signed(client_id="") == ""

    def test_no_private_key_is_no_token(self) -> None:
        assert signed(private_key_pem="") == ""

    def test_neither_is_no_token(self) -> None:
        assert signed(client_id="", private_key_pem="") == ""


class TestAKeyThatWasSetAndIsWrong:
    """The opposite case, and it raises. Somebody put a value there, so somebody wants to hear
    that it does not work rather than watch every repository report as missing."""

    @pytest.mark.parametrize(
        "broken",
        [
            "not a key at all",
            "-----BEGIN RSA PRIVATE KEY-----\ntruncated\n-----END RSA PRIVATE KEY-----\n",
            "-----BEGIN RSA PRIVATE KEY-----\n-----END RSA PRIVATE KEY-----\n",
        ],
    )
    def test_a_key_that_will_not_parse_says_which_setting_is_wrong(self, broken: str) -> None:
        with pytest.raises(GitHubAuthError, match="SHANNON_GITHUB_APP_PRIVATE_KEY"):
            signed(private_key_pem=broken)

    def test_a_key_with_a_passphrase_is_refused_rather_than_hanging(self) -> None:
        """There is nowhere to put a passphrase and nobody to ask for one, so an encrypted key is
        permanently unusable. It parses as a failure, which is the answer wanted."""
        with pytest.raises(GitHubAuthError, match="could not be read"):
            signed(private_key_pem=pem_of(KEY, password=b"hunter2"))

    def test_a_key_that_is_not_rsa_is_refused_by_kind(self) -> None:
        """Its own branch, and it has to be: an Ed25519 key loads perfectly and only fails at the
        moment it is asked for RS256, which is inside the signing call rather than the parse."""
        with pytest.raises(GitHubAuthError, match="not an RSA key"):
            signed(private_key_pem=pem_of(ed25519.Ed25519PrivateKey.generate()))


def test_the_lifetime_and_drift_are_the_numbers_github_documents() -> None:
    """The constants, pinned where the tests above deliberately do not touch them.

    Everything in `TestWhatGitHubChecks` asserts against literals so that moving a constant is
    caught. That leaves the constants themselves ungated, which is what this is for.
    """
    assert CLOCK_DRIFT.total_seconds() == 60
    assert LIFETIME.total_seconds() == 540

    # How far past the real clock the expiry lands, which is the number GitHub actually checks.
    # The backdating spends a minute of the allowance, so this is LIFETIME MINUS the drift and
    # not plus it. Written out because getting the sign wrong looks like caution and is not: it
    # would read as eight minutes of headroom where there are ten, and hide a real overrun.
    ahead = LIFETIME - CLOCK_DRIFT
    assert ahead.total_seconds() == 480
    assert ahead < timedelta(minutes=10), "GitHub refuses a JWT expiring more than ten ahead"
