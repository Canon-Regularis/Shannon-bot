"""What a pull request everybody has approved looks like in a thread. Issue #155.

The exact strings are asserted rather than a substring, the way the draft switch and the state
markers are: this is the whole of what a reader sees, and a mark or a word quietly changing is
the failure this file is for.

The mentions are TEXT, which is the load-bearing part. An allow-list only permits a notification
and the `<@id>` in the body is what delivers one, so a line that resolved the audience and then
said their plain names would look right in every screenshot and ring nobody — which on this line
is the whole feature, because being rung is the point of it.

The count is the other thing worth pinning. Naming the approvers would say nothing the thread
does not already say one message above, and it would say it through a live mention map, so the
person who approved last would be rung about their own approval.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_everyone_approved
from shannon.discord_bot.panels import Accent, BlockKind
from shannon.domain.models import Actor

pytestmark = pytest.mark.unit

OCTOCAT = Actor("octocat", 583231)
MONALISA = Actor("monalisa", 200)
BACKEND = Actor("backend")

HEADING = "### 🏁 Approved"


class TestTheSentence:
    def test_one_approval_reads_as_one_review(self) -> None:
        said = format_everyone_approved(1)

        assert said.text == (
            f"{HEADING}\n1 review, all of them approving, and nobody is still being waited on."
        )

    def test_several_approvals_read_as_several(self) -> None:
        said = format_everyone_approved(3)

        assert said.text == (
            f"{HEADING}\n3 reviews, all of them approving, and nobody is still being waited on."
        )

    def test_it_is_said_even_where_there_is_nobody_to_tell(self) -> None:
        """A pull request whose author's account is gone and which nobody was assigned still
        stopped waiting on its reviewers. The alternative is a thread that says nothing at the
        one moment it most has something to say."""
        said = format_everyone_approved(2, people=(), teams=())

        assert said.text.endswith("nobody is still being waited on.")
        assert not said.text.endswith(" ")


class TestWhoItRings:
    def test_a_linked_person_is_a_live_mention(self) -> None:
        said = format_everyone_approved(1, people=(OCTOCAT,), mentions={"octocat": 4242})

        assert "<@4242>" in said.text

    def test_somebody_nobody_linked_is_still_named(self) -> None:
        """The thread records who a pull request is waiting on even where this server has no way
        to reach them, which is the bargain every other renderer here makes."""
        said = format_everyone_approved(1, people=(MONALISA,), mentions={})

        assert "monalisa" in said.text
        assert "<@" not in said.text

    def test_the_people_come_before_the_sentence(self) -> None:
        """A panel over budget drops blocks from the end, and an allow-list only permits a
        notification while the mention text is what delivers one. So a line trimmed to its
        heading must still carry the people it was sent to ring."""
        said = format_everyone_approved(1, people=(OCTOCAT,), mentions={"octocat": 4242})
        under = next(part for part in said.blocks if part.kind is BlockKind.SUBHEADING)

        assert under.text.startswith("<@4242>")

    def test_everybody_named_is_named_once(self) -> None:
        """The caller dedupes, and this proves the renderer does not undo it by naming somebody
        twice who is both the author and an assignee."""
        said = format_everyone_approved(
            1, people=(OCTOCAT, MONALISA), mentions={"octocat": 1, "monalisa": 2}
        )

        assert said.text.count("<@1>") == 1
        assert said.text.count("<@2>") == 1


class TestTheShapeItShares:
    def test_a_team_is_rendered_through_the_role_map(self) -> None:
        """Accepted so this matches the shape the other two audience-taking renderers use. The
        caller hands an empty sequence, because the audience here is people."""
        said = format_everyone_approved(1, teams=(BACKEND,), roles={"backend": 77})

        assert "<@&77>" in said.text

    def test_it_is_a_card_with_a_heading_and_one_line_under_it(self) -> None:
        said = format_everyone_approved(1, people=(OCTOCAT,))

        assert [part.kind for part in said.blocks] == [BlockKind.HEADING, BlockKind.SUBHEADING]

    def test_it_is_green(self) -> None:
        """`PASSED` rather than `OPEN`, which is the same colour under the name that says what
        this means: a verdict coming back good, rather than a statement about the item."""
        assert format_everyone_approved(1).accent == Accent.PASSED

    def test_a_login_cannot_style_the_sentence_around_it(self) -> None:
        """`_person` escapes now, like `_account` beside it and like everything else this module
        did not write. No login GitHub issues today holds a markdown character, so this guards an
        assumption about GitHub rather than a bug — and the six renderers reading `_person` were
        the one place that assumption was load-bearing.

        Asserted on the raw text rather than on a count of asterisks: an even number of them is
        what an unescaped `**a**` has too, which is how the first version of this test passed
        without proving anything.
        """
        said = format_everyone_approved(1, people=(Actor("a**b"),))

        assert "a**b" not in said.text

    def test_the_mark_is_not_the_one_ci_uses(self) -> None:
        """Both land within a minute of each other on a healthy pull request, and one mark
        meaning two things is what a reader would then have to untangle."""
        assert "✅" not in format_everyone_approved(1).text
