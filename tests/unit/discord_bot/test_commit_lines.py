"""What a commit line says, asserted against the whole string rather than a piece of it.

Issue #67. The three renderers here are the only ones in the project whose output is decided
entirely by somebody outside the server: a commit subject, its body and the account that wrote it
all come off a push nobody on the Discord side approved. So the tests are written the same way the
metadata block's are, against exact text, because a test asking whether the title is "in" the
message would pass on a line that also said something nobody meant it to.

The one thing here that is a rule rather than a rendering is that none of the three takes a
mentions map, which is checked by introspection below. It is requirement 4 of the issue, and it
holds by construction: `_person` is the only thing in `formatting` that builds a `<@id>`.
"""

from __future__ import annotations

import inspect
import re

import pytest

from shannon.discord_bot import formatting
from shannon.discord_bot.panels import PANEL_BUDGET, Accent
from shannon.discord_bot.safe_text import COMMIT_MESSAGE_LIMIT, COMMIT_TITLE_LIMIT
from shannon.domain.models import Actor, Commit, CommitStats

OCTOCAT = Actor(login="octocat", github_user_id=583231)
SHA = "abc1234def5678901234567890123456789012ab"

_ACCOUNT_MENTION = re.compile(r"<@!?\d+>")


def commit(message: str, *, author: Actor | None = OCTOCAT, **stats: int) -> Commit:
    numbers = {"additions": 42, "deletions": 7, "changed_files": 3}
    numbers.update(stats)
    return Commit(sha=SHA, message=message, author=author, stats=CommitStats(**numbers))


class TestWhatOneCommitLineSays:
    def test_a_commit_with_a_body_reads_as_three_blocks(self) -> None:
        said = formatting.format_commit(
            commit("Add the webhook endpoint\n\nAnswers the signature check.")
        ).text

        assert said == (
            "📝 **octocat** has committed Add the webhook endpoint\n"
            "Answers the signature check.\n"
            "-# With changes: +42, -7, 3 files changed"
        )

    def test_a_commit_with_no_body_leaves_the_line_out_rather_than_rendering_it_blank(self) -> None:
        """Most commits are this shape. An empty block between the subject and the numbers
        would draw a rule across nearly every line this bot ever posts."""
        said = formatting.format_commit(
            commit("Fix the flaky reviewer test", additions=3, deletions=3, changed_files=1)
        ).text

        assert said == (
            "📝 **octocat** has committed Fix the flaky reviewer test\n"
            "-# With changes: +3, -3, 1 file changed"
        )

    def test_a_body_of_nothing_but_whitespace_is_no_body(self) -> None:
        said = formatting.format_commit(commit("Tidy up\n\n   \n\t\n")).text

        assert said.splitlines() == [
            "📝 **octocat** has committed Tidy up",
            "-# With changes: +42, -7, 3 files changed",
        ]

    def test_a_commit_whose_account_github_does_not_know_is_still_announced(self) -> None:
        """GitHub answers with no account whenever the committing address is registered to
        nobody, which happens on ordinary work. Dropping the line would silently lose somebody's
        commits for a reason they would have no way of guessing."""
        said = formatting.format_commit(
            commit("Drop the unused import", author=None, additions=0, deletions=1, changed_files=1)
        ).text

        assert said == (
            "📝 **Unknown** has committed Drop the unused import\n"
            "-# With changes: +0, -1, 1 file changed"
        )

    def test_a_commit_with_no_message_at_all_falls_back_to_its_short_sha(self) -> None:
        """`git commit --allow-empty-message` is legal and the parser lets one through, because
        the SHA is the part that had to be there. Without this the line ends on the word
        "committed" and reads as the bot having broken."""
        said = formatting.format_commit(commit("")).text

        assert said.splitlines()[0] == "📝 **octocat** has committed `abc1234`"

    def test_only_the_first_line_is_the_subject(self) -> None:
        """git's own split, and the reason `description` exists. A subject line put next to a
        five-paragraph body would be the whole message on one line."""
        said = formatting.format_commit(commit("Subject\nStraight into the body\nand more")).text

        assert said.splitlines()[0].endswith("has committed Subject")
        assert said.splitlines()[1] == "Straight into the body"


class TestTheNumbersUnderIt:
    def test_one_file_is_one_file(self) -> None:
        said = formatting.format_commit(commit("Tidy", changed_files=1)).text

        assert said.endswith("1 file changed")

    @pytest.mark.parametrize("count", [0, 2, 300])
    def test_anything_else_is_files(self, count: int) -> None:
        said = formatting.format_commit(commit("Tidy", changed_files=count)).text

        assert said.endswith(f"{count} files changed")

    def test_zeroes_are_written_out_rather_than_left_off(self) -> None:
        """A commit that only deletes has no additions, and a line reading `+, -30` looks like a
        renderer that failed rather than a number that happened to be nothing."""
        said = formatting.format_commit(
            commit("Delete the dead module", additions=0, deletions=30, changed_files=2)
        ).text

        assert said.endswith("-# With changes: +0, -30, 2 files changed")


class TestNobodyIsPinged:
    """Requirement 4 of the issue, checked two ways: what the renderers accept, and what they do
    with a body that tries."""

    @pytest.mark.parametrize(
        "render",
        [formatting.format_commit, formatting.format_force_push, formatting.format_commits_left],
    )
    def test_none_of_them_will_even_accept_a_mentions_map(self, render: object) -> None:
        """The guarantee, at the seam where it is enforceable. Every other renderer in this module
        takes a mapping of login to Discord id and that mapping is what makes a `<@id>`; these
        take none, so no amount of wiring can make one of them ping."""
        taken = set(inspect.signature(render).parameters)

        assert "mentions" not in taken
        assert "roles" not in taken

    def test_a_commit_message_that_holds_a_mention_does_not_ring_anybody(self) -> None:
        """Anybody who can push can write `<@1234>` in a commit message. Left alone it resolves
        to a real person in a thread they were never part of."""
        said = formatting.format_commit(commit("Ping <@123456>\n\nAnd <@!654321> as well")).text

        assert not _ACCOUNT_MENTION.search(said)
        assert "123456" in said, "defused rather than deleted, so the text still reads"

    def test_an_everyone_in_a_commit_body_does_not_reach_everyone(self) -> None:
        said = formatting.format_commit(commit("Fix it\n\n@everyone should look at this")).text

        assert "@everyone" not in said

    def test_markdown_in_a_subject_cannot_restyle_the_lines_under_it(self) -> None:
        """The subject sits between two lines built out of matched markers. An odd number of
        asterisks in it bolds the statistics line and whatever Discord shows next."""
        said = formatting.format_commit(commit("Fix **/*.py again")).text

        assert said.splitlines()[0] == "📝 **octocat** has committed Fix \\*\\*/\\*.py again"


class TestWhenSomebodyWritesTooMuch:
    def test_a_subject_one_character_over_the_limit_is_cut(self) -> None:
        """The limit is load-bearing rather than tidiness. A panel over budget drops WHOLE
        BLOCKS from the end, so a subject longer than the budget takes the body and the numbers
        underneath it down with it, and the thread gets half a title and nothing else."""
        said = formatting.format_commit(commit("W" * (COMMIT_TITLE_LIMIT + 1))).text

        assert said.splitlines()[0].endswith("W" * COMMIT_TITLE_LIMIT + "…")

    def test_a_subject_exactly_at_the_limit_is_left_alone(self) -> None:
        said = formatting.format_commit(commit("W" * COMMIT_TITLE_LIMIT)).text

        assert "…" not in said

    def test_a_body_over_the_message_limit_is_cut(self) -> None:
        said = formatting.format_commit(
            commit("Subject\n\n" + "B" * (COMMIT_MESSAGE_LIMIT + 1))
        ).text

        assert said.splitlines()[1] == "B" * COMMIT_MESSAGE_LIMIT + "…"

    def test_the_worst_commit_anybody_could_write_still_fits_in_a_card(self) -> None:
        """Against the literal 3900 rather than the constant, which is the point: asserting
        against `PANEL_BUDGET` compares the output to the very number that decided it.

        Every character is one that escaping doubles, both fields are one over their limits, and
        the numbers are wider than any real repository would produce.
        """
        worst = Commit(
            sha="f" * 40,
            message="*" * (COMMIT_TITLE_LIMIT + 1) + "\n\n" + "_" * (COMMIT_MESSAGE_LIMIT + 1),
            author=Actor(login="_" * 39, github_user_id=1),
            stats=CommitStats(additions=9_999_999, deletions=9_999_999, changed_files=9_999_999),
        )

        assert formatting.format_commit(worst).length() < 3900
        assert formatting.format_commit(worst).length() <= PANEL_BUDGET


class TestABranchThatWasRewritten:
    def test_it_names_who_did_it_and_says_why_nothing_else_follows(self) -> None:
        said = formatting.format_force_push(OCTOCAT).text

        assert said == (
            "🔁 **octocat** force-pushed this branch, so the commits it replaced are not announced."
        )

    def test_a_pusher_github_does_not_know_is_still_reported(self) -> None:
        said = formatting.format_force_push(None).text

        assert said.startswith("🔁 **Unknown** force-pushed")


class TestTheRestOfThePush:
    def test_one_commit_left_is_singular(self) -> None:
        assert formatting.format_commits_left(1).text == (
            "-# 1 earlier commit in this push was not announced."
        )

    def test_more_than_one_is_plural(self) -> None:
        assert formatting.format_commits_left(4).text == (
            "-# 4 earlier commits in this push were not announced."
        )


class TestTheCardItIsDrawnOn:
    def test_a_commit_is_grey_rather_than_coloured(self) -> None:
        """Commits arrive in runs of five and ten. A green bar down each of them turns a push
        into a wall of colour and leaves nothing for the state changes to stand out against.
        """
        assert formatting.format_commit(commit("Tidy")).accent == Accent.NEUTRAL

    def test_the_count_left_over_is_deliberately_not_a_card(self) -> None:
        """A footnote under the last commit. A bar down the side would make the thing being
        apologised for louder than the commits it is apologising about.
        """
        assert formatting.format_commits_left(4).is_plain
