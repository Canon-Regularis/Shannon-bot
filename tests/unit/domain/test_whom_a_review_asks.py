"""Whom a review asking for changes asks, and how that reaches the ring. Issue #263.

The people a message names and the people it rings come from one place, `ChangesAsked.rung`, so
nobody is allow-listed without a visible `<@id>` and nobody visible is left out of the list.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shannon.domain.models import (
    Actor,
    ChangesAsked,
    FileAuthors,
    RepositorySnapshot,
    ReviewSnapshot,
)

pytestmark = pytest.mark.unit

REPOSITORY = RepositorySnapshot(
    github_repo_id=1,
    owner="acme",
    name="widget",
    html_url="https://github.com/acme/widget",
)

OCTOCAT = Actor("octocat", 583231)
HUBOT = Actor("hubot", 100)
MONALISA = Actor("monalisa", 200)


def review(changes_asked: ChangesAsked | None = None) -> ReviewSnapshot:
    return ReviewSnapshot(
        repository=REPOSITORY,
        item_number=7,
        review_id=555,
        html_url="https://github.com/acme/widget/pull/7#pullrequestreview-555",
        body="Not like this.",
        state="changes_requested",
        author=MONALISA,
        created_at=datetime(2026, 8, 11, 11, tzinfo=UTC),
        changes_asked=changes_asked,
    )


class TestWhoIsRung:
    def test_the_authors_of_every_file_are_rung(self) -> None:
        asked = ChangesAsked(
            files=(FileAuthors("a.py", (OCTOCAT,)), FileAuthors("b.py", (HUBOT,))),
        )

        assert asked.rung == (OCTOCAT, HUBOT)

    def test_somebody_who_made_several_of_the_files_is_rung_once(self) -> None:
        """Named on each line they made, and still one person to ring."""
        asked = ChangesAsked(
            files=(
                FileAuthors("a.py", (OCTOCAT, HUBOT)),
                FileAuthors("b.py", (HUBOT,)),
                FileAuthors("c.py", (OCTOCAT,)),
            ),
        )

        assert asked.rung == (OCTOCAT, HUBOT)

    def test_the_same_login_in_another_case_is_the_same_person(self) -> None:
        """GitHub logins are case-insensitive, and the first spelling met is the one kept."""
        shouting = Actor("OCTOCAT", 583231)
        asked = ChangesAsked(
            files=(FileAuthors("a.py", (OCTOCAT,)), FileAuthors("b.py", (shouting,))),
        )

        assert asked.rung == (OCTOCAT,)

    def test_only_the_authors_a_line_names_are_rung(self) -> None:
        """`more` is a count the line says, not people it may ring: an allow-list entry with no
        `<@id>` beside it in the message would ring somebody the message never named."""
        asked = ChangesAsked(files=(FileAuthors("a.py", (OCTOCAT,), more=3),))

        assert asked.rung == (OCTOCAT,)

    def test_files_left_unnamed_ring_nobody(self) -> None:
        """`more_files` is a count the message says, for the same reason `more` is."""
        asked = ChangesAsked(files=(FileAuthors("a.py", (OCTOCAT,)),), more_files=4)

        assert asked.rung == (OCTOCAT,)

    def test_with_no_files_everyone_is_rung(self) -> None:
        asked = ChangesAsked(everyone=(OCTOCAT, HUBOT))

        assert asked.rung == (OCTOCAT, HUBOT)

    def test_everyone_is_rung_once_each_too(self) -> None:
        asked = ChangesAsked(everyone=(OCTOCAT, Actor("Octocat", 583231), HUBOT))

        assert asked.rung == (OCTOCAT, HUBOT)

    def test_files_nobody_can_be_named_for_ring_nobody(self) -> None:
        """Not the `everyone` beside them, which the message does not show when it names files."""
        asked = ChangesAsked(files=(FileAuthors("a.py"),), everyone=(OCTOCAT,))

        assert asked.rung == ()

    def test_asking_nobody_rings_nobody(self) -> None:
        assert ChangesAsked().rung == ()


class TestWhomTheReviewAddresses:
    def test_a_review_nobody_has_worked_out_addresses_nobody(self) -> None:
        """An approval, a plain comment, and a request for changes the mirror has not read for."""
        assert review().replying_to == ()

    def test_a_review_asking_for_changes_addresses_whoever_it_asks(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("a.py", (OCTOCAT, HUBOT)),))

        assert review(asked).replying_to == (OCTOCAT, HUBOT)

    def test_a_review_that_asks_everyone_addresses_everyone(self) -> None:
        assert review(ChangesAsked(everyone=(HUBOT,))).replying_to == (HUBOT,)

    def test_new_fields_default_to_saying_nothing(self) -> None:
        """A review built without them, which every caller before #263 is."""
        built = ReviewSnapshot(
            repository=REPOSITORY,
            item_number=7,
            review_id=1,
            html_url="",
            body="",
            state="approved",
        )

        assert built.commit_id == ""
        assert built.pull_request_author is None
        assert built.changes_asked is None
