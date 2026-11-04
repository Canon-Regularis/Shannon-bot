"""Whether a proof stands behind a link: the one rule `/assign` and `/linked` share. Issue #232.

Held on the account id and never the login, because GitHub hands a freed login to whoever asks
for it next. So a proof follows an account through a rename, and a name changing hands proves
nothing about the person who first linked it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shannon.db.stores.identities import ProvedAccount
from shannon.db.stores.user_links import LinkedAccount

WHEN = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)
LINKED = LinkedAccount(login="octocat", github_user_id=583231)


def signed_in_as(login: str, github_user_id: int) -> ProvedAccount:
    return ProvedAccount(login=login, github_user_id=github_user_id, verified_at=WHEN)


class TestAProofBehindALink:
    def test_a_proof_of_the_account_linked_proves_it(self) -> None:
        assert LINKED.proved_by(signed_in_as("octocat", 583231))

    def test_a_proof_given_under_another_name_for_the_same_account_still_proves_it(self) -> None:
        """The account was renamed between the sign-in and the link. The name moved, and the
        account it names did not."""
        assert LINKED.proved_by(signed_in_as("octocat-before", 583231))

    def test_a_proof_of_another_account_does_not(self) -> None:
        assert not LINKED.proved_by(signed_in_as("monalisa", 200))

    def test_the_same_name_held_by_another_account_does_not(self) -> None:
        """GitHub gave the login to somebody else, and the sign-in was theirs."""
        assert not LINKED.proved_by(signed_in_as("octocat", 999))

    def test_no_proof_at_all_does_not(self) -> None:
        assert not LINKED.proved_by(None)

    def test_a_link_by_name_alone_never_is(self) -> None:
        """A row from before the id column: there is nothing to compare, and reading no evidence
        as a yes is the one wrong answer."""
        by_name = LinkedAccount(login="octocat", github_user_id=None)

        assert not by_name.proved_by(signed_in_as("octocat", 583231))
