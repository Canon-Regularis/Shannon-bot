"""Reading the installation off a delivery, which is how the App knows what it can see.

Issue #98. Not about an item, unlike every other event this bot handles: these say what it is
ABLE to read rather than what happened inside a repository.

The parser is written against the `installation` block rather than against a particular event,
because every App delivery carries that block. That is deliberate and is what lets a directory
which missed a webhook repair itself from ordinary traffic, so it is tested against an `issues`
payload as well as an `installation` one.
"""

from __future__ import annotations

import pytest

from shannon.github.webhooks.events import SUPPORTED_EVENTS, is_supported
from shannon.github.webhooks.installations import parse_installation_event

pytestmark = pytest.mark.unit


def delivery(**overrides: object) -> dict[str, object]:
    installation: dict[str, object] = {
        "id": 42,
        "account": {"login": "octocat", "id": 583231, "type": "User"},
    }
    installation.update(overrides)
    return {"action": "created", "installation": installation}


class TestReadingTheInstallation:
    def test_an_installation_delivery_reads(self) -> None:
        found = parse_installation_event(delivery())

        assert found is not None
        assert (found.installation_id, found.account_login, found.account_id) == (
            42,
            "octocat",
            583231,
        )

    def test_an_ordinary_item_delivery_carries_one_too(self) -> None:
        """Every App delivery does, which is the cheapest repair there is for a directory that
        missed the webhook that would have told it."""
        found = parse_installation_event(
            {
                "action": "opened",
                "issue": {"number": 7},
                "repository": {"id": 1},
                "installation": {"id": 42, "account": {"login": "octocat", "id": 583231}},
            }
        )

        assert found is not None
        assert found.installation_id == 42

    def test_an_organisation_account_reads_the_same_way(self) -> None:
        found = parse_installation_event(
            delivery(account={"login": "acme", "id": 99, "type": "Organization"})
        )

        assert found is not None
        assert found.account_login == "acme"

    def test_an_account_with_no_id_still_reads(self) -> None:
        """The id is allowed to be absent because a row recording a login with no id is useful and
        one recording a made-up id is not."""
        found = parse_installation_event(delivery(account={"login": "octocat"}))

        assert found is not None
        assert found.account_id is None

    @pytest.mark.parametrize("bad", ["", None, "42", [], {}, 4.5])
    def test_an_id_that_is_not_a_number_is_no_installation(self, bad: object) -> None:
        """The id is what a token is minted against, so a value that is not one is unusable rather
        than something to guess around."""
        assert parse_installation_event(delivery(id=bad)) is None

    @pytest.mark.parametrize("bad", [None, {}, {"id": 1}, {"login": ""}, {"login": 7}, "octocat"])
    def test_an_account_with_no_login_is_no_installation(self, bad: object) -> None:
        """The login is the key the directory is looked up by. Without it there is nothing to
        write the row against."""
        assert parse_installation_event(delivery(account=bad)) is None

    @pytest.mark.parametrize("body", [None, "installation", [], 7, {}, {"installation": None}])
    def test_a_delivery_with_no_installation_block_reads_as_nothing(self, body: object) -> None:
        """Which is every delivery from a repository webhook configured by hand. They are ordinary
        during the changeover and must not be treated as broken."""
        assert parse_installation_event(body) is None


class TestWhichDeliveriesReachTheHandler:
    @pytest.mark.parametrize(
        "action", ["created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"]
    )
    def test_every_installation_action_is_acted_on(self, action: str) -> None:
        assert is_supported("installation", action)

    @pytest.mark.parametrize("action", ["added", "removed"])
    def test_repositories_moving_in_or_out_are_acted_on(self, action: str) -> None:
        assert is_supported("installation_repositories", action)

    def test_an_action_github_has_not_invented_yet_is_ignored(self) -> None:
        assert not is_supported("installation", "transferred")

    def test_both_events_are_listed(self) -> None:
        """Against the table rather than through the helper, because a missing key and an empty
        set behave identically through `is_supported` and only one of them is a mistake."""
        assert "installation" in SUPPORTED_EVENTS
        assert "installation_repositories" in SUPPORTED_EVENTS
