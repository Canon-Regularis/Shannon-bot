"""Turning GitHub's JSON into this project's objects, when the JSON is not what it should be.

Every function here guards its fields, and until now nothing checked the guards. The parsers were
covered against payloads missing a key or shaped wrongly at the top, and a mutation campaign found
the gap that leaves: a field that is PRESENT and empty, or present and the wrong type, was let
through by six separate guards without a single test noticing. Turning each
`not isinstance(x, str) or not x` into an `and` left the whole suite green.

What that would cost is not an exception. It is an `Actor` whose login is the empty string
reaching the assignment store and the renderer, a repository with a blank name, and a label with
no text in a thread's tag line, none of which raises anywhere.

Whitespace is deliberately not in the lists below. These guards ask whether a value is a non-empty
string and nothing more, GitHub has no name made of spaces, and asserting a strip here would be
inventing a requirement rather than pinning one.
"""

from __future__ import annotations

import pytest

from shannon.domain.models import RepositorySnapshot
from shannon.github import mapping

REPO = RepositorySnapshot(
    github_repo_id=1, owner="acme", name="widget", html_url="https://github.com/acme/widget"
)

# Present, and no use. GitHub sends none of these today, which is exactly why a guard that stopped
# working would go unnoticed.
UNUSABLE_NAMES = ["", 0, 1, 12.5, None, [], {}, ["x"], {"login": "x"}]
UNUSABLE_URLS = ["", 7, None, [], {}]


class TestAnActor:
    @pytest.mark.parametrize("login", UNUSABLE_NAMES)
    def test_a_login_that_is_not_a_name_is_nobody(self, login: object) -> None:
        assert mapping.actor({"login": login, "id": 7}) is None

    def test_a_usable_login_still_maps(self) -> None:
        found = mapping.actor({"login": "octocat", "id": 7})

        assert found is not None
        assert (found.login, found.github_user_id) == ("octocat", 7)

    def test_an_id_that_is_not_a_number_is_dropped_without_losing_the_actor(self) -> None:
        """The login is what everything downstream matches on; the id is a convenience."""
        found = mapping.actor({"login": "octocat", "id": "seven"})

        assert found is not None
        assert found.github_user_id is None


class TestATeam:
    @pytest.mark.parametrize("slug", UNUSABLE_NAMES)
    def test_a_slug_that_is_not_a_name_is_no_team(self, slug: object) -> None:
        assert mapping.team({"slug": slug}) is None

    def test_the_name_is_read_only_when_there_is_no_slug(self) -> None:
        """A slug is stable; a name is a display string somebody can change."""
        assert mapping.team({"slug": "backend", "name": "Backend Team"}).login == "backend"
        assert mapping.team({"name": "Backend Team"}).login == "Backend Team"


class TestARepository:
    @pytest.mark.parametrize("name", UNUSABLE_NAMES)
    def test_a_name_that_is_not_a_name_is_no_repository(self, name: object) -> None:
        assert mapping.repository({"id": 5, "name": name, "owner": {"login": "acme"}}) is None

    @pytest.mark.parametrize("repo_id", ["5", "", None, 5.0, [], {}])
    def test_an_id_that_is_not_a_number_is_no_repository(self, repo_id: object) -> None:
        """The id is what an inbound webhook is resolved to a guild by, so a wrong one is worse
        than none: it would file the delivery under whichever repository happened to match."""
        payload = {"id": repo_id, "name": "widget", "owner": {"login": "acme"}}

        assert mapping.repository(payload) is None

    @pytest.mark.parametrize("url", UNUSABLE_URLS)
    def test_an_unusable_link_falls_back_to_one_that_works(self, url: object) -> None:
        found = mapping.repository(
            {"id": 5, "name": "widget", "owner": {"login": "acme"}, "html_url": url}
        )

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget"


class TestLabels:
    @pytest.mark.parametrize("name", UNUSABLE_NAMES)
    def test_a_label_with_no_usable_name_is_dropped_rather_than_rendered_blank(
        self, name: object
    ) -> None:
        assert mapping.labels([{"name": name}]) == ()

    def test_the_usable_ones_survive_beside_it(self) -> None:
        found = mapping.labels([{"name": "bug"}, {"name": ""}, {"name": "backend"}])

        assert [label.name for label in found] == ["bug", "backend"]


class TestAnItem:
    @pytest.mark.parametrize("url", UNUSABLE_URLS)
    def test_an_unusable_link_is_rebuilt_from_the_number(self, url: object) -> None:
        """The link is the one field of the metadata block a reader clicks, so a blank one is
        the difference between a thread that is useful and a thread that is not."""
        found = mapping.issue({"id": 5, "number": 7, "html_url": url}, REPO)

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget/issues/7"

    def test_a_pull_request_is_rebuilt_under_its_own_path(self) -> None:
        found = mapping.pull_request({"id": 5, "number": 7, "html_url": ""}, REPO)

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget/pull/7"
