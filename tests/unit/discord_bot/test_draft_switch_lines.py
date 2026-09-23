"""What a pull request crossing into or out of draft looks like in a thread.

The exact strings are asserted rather than a substring of them, the way the state markers are:
these are the whole of what a reader sees, and a mark or a word quietly changing is the failure
this file is for.

The mentions are TEXT, which is the one thing here that is load-bearing beyond the wording. An
allow-list only permits a notification; the `<@id>` in the body is what delivers one. A line that
resolved the audience and then said their plain names would look right in every screenshot and
ring nobody.

The other direction matters on the way back into draft, and only this file can prove it. A role
mention rings everybody holding the role whatever the allow-list says, so the draft line names a
team in plain text — and the only way to show that is to hand the renderer a populated role map
and watch it decline to use it. Issues #132 and #140.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_back_to_draft, format_ready_for_review
from shannon.discord_bot.panels import Accent, BlockKind
from shannon.domain.models import Actor

pytestmark = pytest.mark.unit

OCTOCAT = Actor("octocat", 583231)
MONALISA = Actor("monalisa", 200)
HUBOT = Actor("hubot", 100)
BACKEND = Actor("backend")

HEADING = "### 🟢 Ready for review"
MARKED_IT = "**octocat** marked this pull request ready for review."
DRAFT_HEADING = "### 🚧 Back to draft"
DRAFTED_IT = "**octocat** converted this pull request to draft."


class TestTheSentence:
    def test_it_names_whoever_pressed_the_button(self) -> None:
        assert format_ready_for_review(OCTOCAT).text == f"{HEADING}\n{MARKED_IT}"

    def test_it_is_said_even_where_there_is_nobody_to_tell(self) -> None:
        """A pull request with no reviewers and no assignees still left draft. The alternative is
        a thread that says nothing at the one moment it most has something to say."""
        said = format_ready_for_review(OCTOCAT, people=(), teams=())

        assert said.text == f"{HEADING}\n{MARKED_IT}"

    def test_an_account_that_has_gone_is_named_as_unknown(self) -> None:
        """GitHub sends a null sender for a deleted account, and the parser answers None for one.
        The line is still worth saying: the pull request is still ready."""
        assert format_ready_for_review(None).text == (
            f"{HEADING}\n**Unknown** marked this pull request ready for review."
        )

    def test_a_login_cannot_style_the_sentence_around_it(self) -> None:
        """The one piece of somebody else's text in this line. A login carrying asterisks would
        otherwise close the bold early and restyle everything after it."""
        said = format_ready_for_review(Actor("**not-really**"))

        assert said.text.endswith("marked this pull request ready for review.")
        assert said.text.count("**") == 2, "the login opened styling of its own"


class TestWhoIsRung:
    def test_a_linked_person_is_a_mention(self) -> None:
        said = format_ready_for_review(OCTOCAT, people=(MONALISA,), mentions={"monalisa": 555})

        assert said.text == f"{HEADING}\n<@555> {MARKED_IT}"

    def test_an_unlinked_person_is_still_named(self) -> None:
        """The same bargain every other renderer makes: the thread records who was asked even
        where nobody has run /link for them."""
        said = format_ready_for_review(OCTOCAT, people=(MONALISA,))

        assert said.text == f"{HEADING}\nmonalisa {MARKED_IT}"

    def test_a_team_is_a_role_mention(self) -> None:
        """A different syntax from a person's, and getting it wrong is silent: `<@123>` for a
        role id resolves to nobody and renders as a broken mention rather than as an error."""
        said = format_ready_for_review(OCTOCAT, teams=(BACKEND,), roles={"backend": 777})

        assert said.text == f"{HEADING}\n<@&777> {MARKED_IT}"

    def test_an_unlinked_team_is_still_named(self) -> None:
        said = format_ready_for_review(OCTOCAT, teams=(BACKEND,))

        assert said.text == f"{HEADING}\nbackend {MARKED_IT}"

    def test_everybody_comes_before_the_sentence(self) -> None:
        """People first, then teams, then what happened. The order is what a reader scans."""
        said = format_ready_for_review(
            OCTOCAT,
            people=(MONALISA, HUBOT),
            teams=(BACKEND,),
            mentions={"monalisa": 555, "hubot": 606},
            roles={"backend": 777},
        )

        assert said.text == f"{HEADING}\n<@555> <@606> <@&777> {MARKED_IT}"


class TestTheShape:
    def test_it_is_green(self) -> None:
        """The colour the card turns in the same breath. The two disagreeing would be the
        reader's problem rather than this module's."""
        assert format_ready_for_review(OCTOCAT).accent is Accent.OPEN

    def test_the_heading_comes_first_and_only_once(self) -> None:
        kinds = [block.kind for block in format_ready_for_review(OCTOCAT, people=(HUBOT,)).blocks]

        assert kinds[0] is BlockKind.HEADING
        assert BlockKind.HEADING not in kinds[1:]

    def test_the_people_are_in_the_block_under_the_heading(self) -> None:
        """Not decoration. A panel over budget drops blocks from the end, so the names have to be
        as near the top as the heading allows or a trimmed card rings nobody."""
        blocks = format_ready_for_review(OCTOCAT, people=(MONALISA,), mentions={"monalisa": 555})

        assert blocks.blocks[1].kind is BlockKind.SUBHEADING
        assert "<@555>" in blocks.blocks[1].text


class TestTheDraftSentence:
    """The mirror of the first class. Issue #140 reversed the decision to say nothing here."""

    def test_it_names_whoever_pressed_the_button(self) -> None:
        assert format_back_to_draft(OCTOCAT).text == f"{DRAFT_HEADING}\n{DRAFTED_IT}"

    def test_it_is_said_even_where_there_is_nobody_to_tell(self) -> None:
        """A pull request with nobody on it still went back into draft, and the thread is the
        only record that it did."""
        assert format_back_to_draft(OCTOCAT, people=(), teams=()).text == (
            f"{DRAFT_HEADING}\n{DRAFTED_IT}"
        )

    def test_an_account_that_has_gone_is_named_as_unknown(self) -> None:
        assert format_back_to_draft(None).text == (
            f"{DRAFT_HEADING}\n**Unknown** converted this pull request to draft."
        )

    def test_a_login_cannot_style_the_sentence_around_it(self) -> None:
        """The one piece of somebody else's text in this line, escaped like every other login."""
        said = format_back_to_draft(Actor("**not-really**"))

        assert said.text.endswith("converted this pull request to draft.")
        assert said.text.count("**") == 2, "the login opened styling of its own"

    def test_the_two_halves_use_github_s_own_verbs(self) -> None:
        """`marked ready for review` and `converted to draft` are what the buttons are called, so
        the pair reads as one system rather than as two people's wording."""
        assert "marked this pull request ready for review" in format_ready_for_review(OCTOCAT).text
        assert "converted this pull request to draft" in format_back_to_draft(OCTOCAT).text


class TestWhoIsToldItWentBack:
    def test_a_linked_person_is_a_mention(self) -> None:
        said = format_back_to_draft(OCTOCAT, people=(MONALISA,), mentions={"monalisa": 555})

        assert said.text == f"{DRAFT_HEADING}\n<@555> {DRAFTED_IT}"

    def test_an_unlinked_person_is_still_named(self) -> None:
        assert format_back_to_draft(OCTOCAT, people=(MONALISA,)).text == (
            f"{DRAFT_HEADING}\nmonalisa {DRAFTED_IT}"
        )

    def test_a_linked_team_is_named_and_not_rung(self) -> None:
        """The whole of what separates this line from the one above, and the only place it can be
        proved: the role map is populated and the renderer declines to use it.

        A role mention reaches everybody holding the role no matter what the allow-list says, and
        Discord gives nobody a way to leave one person out of one. Asking a team to look is worth
        waking them for. Telling them to stop looking is not.
        """
        said = format_back_to_draft(OCTOCAT, teams=(BACKEND,), roles={"backend": 777})

        assert said.text == f"{DRAFT_HEADING}\nbackend {DRAFTED_IT}"
        assert "<@&" not in said.text

    def test_the_ready_line_does_ring_that_same_team(self) -> None:
        """Beside the test above rather than in the other class, because the pair IS the claim:
        one mapping, two renderers, two answers."""
        said = format_ready_for_review(OCTOCAT, teams=(BACKEND,), roles={"backend": 777})

        assert said.text == f"{HEADING}\n<@&777> {MARKED_IT}"

    def test_everybody_comes_before_the_sentence(self) -> None:
        said = format_back_to_draft(
            OCTOCAT,
            people=(MONALISA, HUBOT),
            teams=(BACKEND,),
            mentions={"monalisa": 555, "hubot": 606},
            roles={"backend": 777},
        )

        assert said.text == f"{DRAFT_HEADING}\n<@555> <@606> backend {DRAFTED_IT}"


class TestTheShapeOfTheDraftLine:
    def test_it_is_grey(self) -> None:
        """The colour the card turns in the same breath. Asserted as a value rather than by name,
        because `Accent.DRAFT` and `Accent.NEUTRAL` are one int and the name would prove less
        than it looks: what is pinned here is grey, not which word was typed."""
        assert format_back_to_draft(OCTOCAT).accent == Accent.DRAFT

    def test_the_heading_comes_first_and_only_once(self) -> None:
        kinds = [block.kind for block in format_back_to_draft(OCTOCAT, people=(HUBOT,)).blocks]

        assert kinds[0] is BlockKind.HEADING
        assert BlockKind.HEADING not in kinds[1:]

    def test_the_people_are_in_the_block_under_the_heading(self) -> None:
        """A panel over budget drops blocks from the end, so the names have to sit as near the
        top as the heading allows or a trimmed card tells nobody."""
        said = format_back_to_draft(OCTOCAT, people=(MONALISA,), mentions={"monalisa": 555})

        assert said.blocks[1].kind is BlockKind.SUBHEADING
        assert "<@555>" in said.blocks[1].text
