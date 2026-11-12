"""Making sure this bot's own labels exist on a repository, named and coloured right. Issue #246.

What matters most is the rename in place: a repository that has carried `HIGH` for months is
handed `priority: High` by renaming that label, which moves it on every item at once, rather than
by making a second label beside it and leaving the old one on everything it was ever put on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shannon.domain.enums import Priority
from shannon.github.errors import (
    GitHubAuthError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.labels import PRIORITY_LABELS, LabelDefinition, status_label
from shannon.services.label_definitions import LIFETIME, LabelDefinitions
from tests.fakes.github import FakeGitHubClient

REPO = "acme/widget"
HIGH = PRIORITY_LABELS[Priority.HIGH]


def github_with(labels: dict[str, str]) -> FakeGitHubClient:
    github = FakeGitHubClient()
    github.label_colours_by_repo[REPO] = dict(labels)
    return github


async def ensure(github: FakeGitHubClient, definition: LabelDefinition = HIGH) -> None:
    await LabelDefinitions(github).ensure("acme", "widget", definition)


class BeatenToIt(FakeGitHubClient):
    """A repository somebody gives the label to between this listing it and making it."""

    async def create_label(self, owner: str, name: str, label: str, colour: str) -> None:
        self.label_colours_by_repo[f"{owner}/{name}"][label] = "ededed"
        raise GitHubRefusedError("already_exists")


class TestARepositoryWithoutTheLabel:
    async def test_it_is_made_in_its_colour(self) -> None:
        github = github_with({"bug": "d73a4a"})

        await ensure(github)

        assert github.definition_calls == [("create", REPO, "priority: High", "", "d73a4a")]
        assert github.label_colours_by_repo[REPO]["priority: High"] == "d73a4a"

    async def test_one_made_by_somebody_else_a_moment_ago_is_recoloured(self) -> None:
        """Listed without it, refused as already there: GitHub's 422. The label exists, so all
        that can be wrong with it is the colour."""
        github = BeatenToIt()
        github.label_colours_by_repo[REPO] = {}

        await ensure(github)

        assert github.definition_calls == [
            ("update", REPO, "priority: High", "priority: High", "d73a4a")
        ]
        assert github.label_colours_by_repo[REPO] == {"priority: High": "d73a4a"}


class TestTheRenameInPlace:
    async def test_the_label_this_bot_used_to_write_becomes_the_new_one(self) -> None:
        github = github_with({"HIGH": "ededed", "bug": "d73a4a"})
        github.labels[(REPO, 7)] = ["HIGH", "bug"]
        github.labels[(REPO, 8)] = ["HIGH"]

        await ensure(github)

        assert github.definition_calls == [("update", REPO, "HIGH", "priority: High", "d73a4a")]
        assert github.labels[(REPO, 7)] == ["priority: High", "bug"]
        assert github.labels[(REPO, 8)] == ["priority: High"], "every item moves at once"

    async def test_it_is_found_whatever_its_case(self) -> None:
        """GitHub matches a label name without regard to case, so `high` made by hand is the label
        this bot was writing to all along."""
        github = github_with({"High": "00ff00"})

        await ensure(github)

        assert github.definition_calls == [("update", REPO, "High", "priority: High", "d73a4a")]

    async def test_a_status_takes_over_the_old_label_for_its_status(self) -> None:
        github = github_with({"IN_REVIEW": "ededed"})

        await ensure(github, status_label("In progress", board_colour="YELLOW"))

        assert github.definition_calls == [
            ("update", REPO, "IN_REVIEW", "status: In progress", "d4a72c")
        ]

    async def test_with_the_new_label_there_already_the_old_one_is_left(self) -> None:
        """Renaming onto a name the repository has would be refused, and the old label comes off
        each item as it is next set instead."""
        github = github_with({"HIGH": "ededed", "priority: High": "d73a4a"})

        await ensure(github)

        assert github.definition_calls == []


class TestALabelThatIsThereAlready:
    async def test_the_right_name_in_the_right_colour_is_left_alone(self) -> None:
        github = github_with({"priority: High": "D73A4A"})

        await ensure(github)

        assert github.definition_calls == []

    async def test_the_wrong_colour_is_put_right(self) -> None:
        github = github_with({"priority: High": "ededed"})

        await ensure(github)

        assert github.definition_calls == [
            ("update", REPO, "priority: High", "priority: High", "d73a4a")
        ]

    async def test_the_wrong_case_is_put_right(self) -> None:
        github = github_with({"PRIORITY: HIGH": "d73a4a"})

        await ensure(github)

        assert github.definition_calls == [
            ("update", REPO, "PRIORITY: HIGH", "priority: High", "d73a4a")
        ]


class TestAskingGitHubNoMoreThanItHasTo:
    async def test_a_label_found_right_is_not_asked_about_again(self) -> None:
        github = github_with({})
        definitions = LabelDefinitions(github)

        await definitions.ensure("acme", "widget", HIGH)
        await definitions.ensure("Acme", "Widget", HIGH)

        assert github.label_colour_reads == [REPO]

    @pytest.mark.parametrize(
        "other",
        [
            LabelDefinition("priority: high", "d73a4a"),
            LabelDefinition("priority: High", "ededed"),
        ],
        ids=["spelled", "coloured"],
    )
    async def test_a_label_wanted_differently_is_asked_about_again(
        self, other: LabelDefinition
    ) -> None:
        """Settled is for the label exactly as wanted. Another spelling or colour of it, put
        right a moment ago, is asked about rather than taken as done."""
        github = github_with({})
        definitions = LabelDefinitions(github)

        await definitions.ensure("acme", "widget", HIGH)
        await definitions.ensure("acme", "widget", other)

        assert len(github.label_colour_reads) == 2

    async def test_each_label_is_its_own_question(self) -> None:
        github = github_with({})
        definitions = LabelDefinitions(github)

        await definitions.ensure("acme", "widget", HIGH)
        await definitions.ensure("acme", "widget", PRIORITY_LABELS[Priority.LOW])

        assert len(github.label_colour_reads) == 2

    async def test_it_looks_again_once_the_life_runs_out(self) -> None:
        """A label recoloured or deleted by hand is put right again, an hour on at most."""
        github = github_with({})
        clock = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        definitions = LabelDefinitions(github, now=lambda: clock)

        await definitions.ensure("acme", "widget", HIGH)
        clock = clock + LIFETIME + timedelta(seconds=1)
        await definitions.ensure("acme", "widget", HIGH)

        assert len(github.label_colour_reads) == 2


class TestAFailureIsNeverTheWritesProblem:
    """A label in the wrong colour is cosmetic, and refusing a status change over one would be
    the wrong way round. GitHub makes the label grey when it is put on, as it always did."""

    @pytest.mark.parametrize(
        "error",
        [GitHubUnavailableError("down"), GitHubAuthError("read only"), GitHubRefusedError("no")],
    )
    async def test_it_is_logged_and_swallowed(
        self, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        github = github_with({})
        github.write_error = error

        await ensure(github)

        assert "will make it grey" in caplog.text

    async def test_a_failure_is_asked_about_again_next_time(self) -> None:
        """Not remembered as settled, so the next write gets another go."""
        github = github_with({})
        github.error = GitHubUnavailableError("down")
        definitions = LabelDefinitions(github)

        await definitions.ensure("acme", "widget", HIGH)
        github.error = None
        await definitions.ensure("acme", "widget", HIGH)

        assert github.label_colours_by_repo[REPO] == {"priority: High": "d73a4a"}
