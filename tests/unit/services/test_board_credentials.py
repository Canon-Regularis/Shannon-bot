"""The cipher over a board authorisation, and what an unusable key means.

Issue #170. The round trip that stores one is tested against a real database in
`tests/integration/test_linking_through_github.py`; what is here is the half that needs no
database at all - whether a deployment can keep a credential, and what it says when it cannot.

The rule these pin is that a key problem must never be a startup problem. A lock added over a
working system must not become the reason the system stops: taking webhooks, deliveries and every
other server down over a board setting would be a far worse failure than the one it guards against.
So an unusable key answers "boards are off", loudly, and the process carries on.
"""

from __future__ import annotations

import pytest

from shannon.services.board_credentials import BoardCredentials, _cipher
from tests.support.credentials import BOARD_KEY, OTHER_BOARD_KEY


class TestWhetherADeploymentCanKeepOne:
    """`usable` is asked before a command offers the round trip, so that somebody is told what is
    missing rather than sent to GitHub to grant something that cannot then be stored."""

    def test_a_real_key_can(self) -> None:
        assert BoardCredentials(None, keys=BOARD_KEY).usable is True  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "keys",
        ["", "   ", ",", ",,", "not-a-fernet-key", "'quoted'"],
        ids=["unset", "whitespace", "comma", "commas", "prose", "quoted"],
    )
    def test_nothing_usable_cannot(self, keys: str) -> None:
        """Every shape of nothing, and the one that actually happens: a key pasted with the quotes
        still around it, which is a string Fernet refuses rather than a string of the wrong
        length."""
        assert BoardCredentials(None, keys=keys).usable is False  # type: ignore[arg-type]

    def test_a_usable_key_beside_a_broken_one_still_works(self) -> None:
        """A list is edited by hand during a rotation, so one bad entry must not take the good one
        with it. The broken entry is dropped and said; the deployment keeps working."""
        assert (
            BoardCredentials(None, keys=f"{BOARD_KEY},oops").usable is True  # type: ignore[arg-type]
        )


class TestWhatAnUnusableKeyIsReportedAs:
    def test_a_broken_key_is_said_once_and_names_the_setting(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Naming the setting rather than the key, because the key is the secret and the setting
        is the thing somebody can go and look at. The line carries the command that generates a
        good one, since that is the next thing they need."""
        with caplog.at_level("ERROR"):
            _cipher("not-a-fernet-key")

        assert caplog.text.count("SHANNON_BOARD_CREDENTIAL_KEY") == 1
        assert "Fernet.generate_key()" in caplog.text

    def test_a_broken_key_is_never_itself_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A malformed key is still somebody's attempt at a secret, and the commonest malformation
        is a real key with punctuation around it."""
        with caplog.at_level("DEBUG"):
            _cipher(f"'{BOARD_KEY}'")

        assert BOARD_KEY not in caplog.text

    def test_nothing_at_all_is_not_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A deployment that uses no board has no key, and that is not a misconfiguration. Only a
        key that was meant to work and does not is worth a line."""
        with caplog.at_level("ERROR"):
            _cipher("")

        assert caplog.text == ""


class TestTheCipherItself:
    def test_a_token_round_trips(self) -> None:
        cipher = _cipher(BOARD_KEY)
        assert cipher is not None
        assert cipher.decrypt(cipher.encrypt(b"gho_abc")) == b"gho_abc"

    def test_the_ciphertext_is_not_the_token(self) -> None:
        cipher = _cipher(BOARD_KEY)
        assert cipher is not None
        assert b"gho_abc" not in cipher.encrypt(b"gho_abc")

    def test_the_same_token_encrypts_differently_each_time(self) -> None:
        """Fernet carries a random IV, so two rows holding the same token do not look alike. Worth
        asserting rather than assuming: equal ciphertexts would tell anybody reading the table
        which people had authorised the same account."""
        cipher = _cipher(BOARD_KEY)
        assert cipher is not None
        assert cipher.encrypt(b"gho_abc") != cipher.encrypt(b"gho_abc")

    def test_the_first_key_is_the_one_that_writes(self) -> None:
        """Newest first, which is what makes a rotation one deploy rather than everybody
        authorising again."""
        rotated = _cipher(f"{OTHER_BOARD_KEY},{BOARD_KEY}")
        newest = _cipher(OTHER_BOARD_KEY)
        assert rotated is not None
        assert newest is not None
        assert newest.decrypt(rotated.encrypt(b"gho_abc")) == b"gho_abc"
