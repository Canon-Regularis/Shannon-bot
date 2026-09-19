"""What a CI result looks like in a thread. Issue #112.

Two things here are load-bearing in a way the other renderers are not.

The ping is TEXT. An allow-list only permits a notification; the `<@id>` in the body is what
delivers one, and a panel over budget drops blocks from the end. So a card trimmed down to its
heading has to still carry the people it was sent to ring, which is why they are the second block
and why there is a test that makes the lists enormous and checks they survive.

And the job names come from GitHub, so they are somebody else's text in a message this bot signs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from shannon.discord_bot.formatting import (
    JOBS_LISTED,
    JOBS_NAMED,
    format_check_results,
)
from shannon.discord_bot.panels import PANEL_BUDGET, Accent
from shannon.discord_bot.safe_text import JOB_NAME_LIMIT, JOB_NAME_LIMIT_JOINED
from shannon.domain.models import Actor, CheckReport, CheckRun

pytestmark = pytest.mark.unit

ZWSP = "​"


def run(
    number: int = 1,
    name: str = "Lint",
    conclusion: str = "success",
    *,
    url: str = "https://github.com/acme/widget/actions/runs/1/job/1",
) -> CheckRun:
    return CheckRun(
        check_run_id=number, name=name, status="completed", conclusion=conclusion, html_url=url
    )


def report(*runs: CheckRun) -> CheckReport:
    return CheckReport(sha="c" * 40, runs=runs)


def words(
    report: CheckReport,
    *,
    people: Sequence[Actor] = (),
    teams: Sequence[Actor] = (),
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> str:
    """What the card says, in reading order, which is what most of this file is about.

    The renderer's own signature rather than a pass-through, so a parameter renamed on one
    side of it fails here instead of quietly arriving as something else.
    """
    return format_check_results(
        report, people=people, teams=teams, mentions=mentions, roles=roles
    ).text


GREEN = report(run(1, "Lint"), run(2, "Tests", "success"))
RED = report(run(1, "Lint"), run(2, "Tests", "failure"), run(3, "Publish", "skipped"))


class TestTheHeadline:
    def test_a_pass_counts_what_succeeded_against_everything(self) -> None:
        assert format_check_results(GREEN).blocks[0].text == ("### ✅ 2 / 2 jobs have succeeded.")

    def test_a_failure_says_so_in_the_first_line(self) -> None:
        """A heading, like the state changes, because a broken build is what somebody scrolls a
        thread looking for."""
        assert format_check_results(RED).blocks[0].text == ("### ❌ 1 / 3 jobs have succeeded.")

    def test_a_single_job_reads_as_one(self) -> None:
        """The verb moves with the noun. `1 / 1 job have succeeded` is what happens when only
        half of the pluralisation is done."""
        assert format_check_results(report(run())).blocks[0].text == (
            "### ✅ 1 / 1 job has succeeded."
        )

    def test_the_skipped_job_does_not_count_as_a_failure(self) -> None:
        """The whole reason there are three buckets. `Publish` is skipped on every pull request
        here, and counting it as broken would report a failure on every green build."""
        said = format_check_results(report(run(1, "CI"), run(2, "Publish", "skipped")))

        assert said.blocks[0].text.startswith("### ✅")
        assert said.accent == Accent.PASSED, "a green build with a skip in it read as broken"
        assert "Unsuccessful" not in said.text


class TestWhoIsRung:
    def test_the_people_are_the_second_block(self) -> None:
        """Above every list, because a panel over budget drops blocks from the end and a
        trimmed mention rings nobody however the allow-list was built."""
        said = format_check_results(GREEN, people=[Actor("alice")], mentions={"alice": 111})

        assert said.blocks[1].text.startswith("<@111>")

    def test_they_survive_a_message_that_has_to_be_trimmed(self) -> None:
        """The failure list alone has to overflow the whole budget, or this proves nothing: with a
        message that fits, the ping survives wherever it is put and the ordering is never tested.

        A job's log URL is GitHub's to shape and nothing here clips one, so a handful of long ones
        is all it takes.
        """
        long_url = "https://github.com/o/r/actions/runs/" + "9" * 900
        huge = report(
            *(run(n, f"A job name number {n:04}", "failure", url=long_url) for n in range(20))
        )

        said = format_check_results(huge, people=[Actor("alice")], mentions={"alice": 111})
        cut = said.trimmed()

        assert said.length() > PANEL_BUDGET, (
            "the lists were not big enough to be trimmed, so this proves nothing"
        )
        assert cut.length() <= PANEL_BUDGET
        assert "<@111>" in cut.text, "the ping was trimmed off, so it would ring nobody"

    def test_somebody_with_no_link_is_still_named(self) -> None:
        said = words(GREEN, people=[Actor("bob")], mentions={})

        assert "bob" in said
        assert "<@" not in said

    def test_a_linked_team_is_a_role_mention(self) -> None:
        said = words(GREEN, teams=[Actor("core")], roles={"core": 999})

        assert "<@&999>" in said

    def test_nobody_given_still_says_what_happened(self) -> None:
        """Which is a draft: the results are posted and nobody is rung."""
        said = words(GREEN)

        assert "Everything that ran passed." in said
        assert "<@" not in said

    def test_a_renderer_with_no_map_cannot_build_a_mention(self) -> None:
        """The guarantee the commit lines rest on, restated here. `_person` needs a map to look an
        account up in, so people named without one come out as text."""
        said = words(RED, people=[Actor("alice")], mentions=None)

        assert "<@" not in said
        assert "alice" in said

    def test_the_verdict_differs_between_the_two_outcomes(self) -> None:
        assert "did not pass" in words(RED)
        assert "Everything that ran passed" in words(GREEN)


class TestTheLists:
    def test_failures_come_before_successes(self) -> None:
        """`fit` sheds from the end, so the list somebody has to act on goes first."""
        said = words(RED)

        assert said.index("Unsuccessful Jobs") < said.index("Successful Jobs")

    def test_a_failure_carries_a_link_to_its_log(self) -> None:
        said = words(report(run(1, "Tests", "failure", url="https://x/job/9")))

        assert "- `Tests` <https://x/job/9>" in said

    def test_a_failure_with_no_link_still_names_the_job(self) -> None:
        said = words(report(run(1, "Tests", "failure", url="")))

        assert "- `Tests`" in said

    def test_successes_are_names_on_one_line(self) -> None:
        """A link line runs about a hundred and eighty characters, so thirty of them would be more
        than the whole message and `fit` would be choosing which failures a reader sees."""
        said = words(report(run(1, "Lint"), run(2, "Tests")))

        assert "**Successful Jobs:** `Lint`, `Tests`" in said
        assert "https://" not in said

    def test_the_third_bucket_is_counted_rather_than_named(self) -> None:
        said = words(report(run(1, "CI"), run(2, "Publish", "skipped")))

        assert "-# 1 other job neither passed nor failed." in said
        assert "Publish" not in said

    def test_a_long_list_of_failures_is_cut_and_says_how_many(self) -> None:
        many = report(*(run(n, f"Job {n}", "failure") for n in range(JOBS_LISTED + 5)))

        said = words(many)

        assert "-# and 5 more that did not pass." in said

    def test_a_long_list_of_successes_is_cut_and_says_how_many(self) -> None:
        many = report(*(run(n, f"Job {n}") for n in range(JOBS_NAMED + 3)))

        said = words(many)

        assert ", and 3 more" in said

    def test_the_footnote_counts_from_the_real_total_not_the_cut_list(self) -> None:
        """Computed separately from the slice, so a message could list ten and correctly claim
        five hidden while showing all fifteen."""
        many = report(*(run(n, f"Job {n:02}", "failure") for n in range(JOBS_LISTED + 5)))

        said = words(many)

        assert f"Job {JOBS_LISTED - 1:02}" in said, "the ones listed should be the first few"
        assert f"Job {JOBS_LISTED:02}" not in said, "everything past the cut is still in there"


class TestSomebodyElsesText:
    def test_a_job_named_as_a_mention_cannot_ring_anybody(self) -> None:
        said = words(report(run(1, "<@1234>", "failure")))

        # The zero-width space goes INSIDE the brackets, which is where it stops Discord
        # resolving the id. A code span alone would not: the span stops markdown reading the
        # name, not Discord reading a mention.
        assert "<" + ZWSP + "@1234>" in said
        assert "<@1234>" not in said

    @pytest.mark.parametrize("name", ["a``b", "a```b", "```", "`" * 20])
    def test_a_job_full_of_backticks_cannot_open_a_code_block(self, name: str) -> None:
        """Three backticks open a BLOCK in Discord rather than a longer inline span, so a name
        carrying them would turn the rest of the message into whatever came next."""
        said = words(report(run(1, name, "failure")))

        assert "```" not in said

    def test_a_very_long_job_name_is_cut_rather_than_carried(self) -> None:
        """Asserted on the cut rather than on the message fitting. One long name fits either way,
        so a test that only checked the length passed with the clip removed."""
        said = words(report(run(1, "n" * 300, "failure", url="https://x/job/9")))

        assert "n" * 300 not in said, "the name reached the line at full length"
        assert "n" * JOB_NAME_LIMIT not in said, "cut, but not to the limit it was given"
        assert "https://x/job/9" in said

    def test_a_long_name_keeps_the_end_that_tells_it_apart(self) -> None:
        """A matrix names its jobs `Tests (Python 3.12)` and `Tests (Python 3.13)`, so cutting the
        other way would render a dozen distinct failures as the same line."""
        said = words(report(run(1, "Tests " + "x" * JOB_NAME_LIMIT + " (Python 3.14)", "failure")))

        assert "(Python 3.14)" in said

    def test_a_long_name_in_the_joined_list_is_cut_harder(self) -> None:
        said = words(report(run(1, "y" * 300)))

        assert "y" * (JOB_NAME_LIMIT_JOINED + 1) not in said


def test_the_worst_report_still_fits_a_card_with_its_ping_intact() -> None:
    """Trimming is the panel's job now rather than the renderer's, so what is asserted is what
    survives it: the card fits, and the two blocks that had to survive did.
    """
    worst = report(
        *(run(n, "n" * 300, "failure" if n % 2 else "success") for n in range(100)),
    )

    said = format_check_results(worst, people=[Actor("alice")], mentions={"alice": 111})
    cut = said.trimmed()

    assert cut.length() <= PANEL_BUDGET
    assert cut.length() < 3900, "asserted against the literal as well, not only the constant"
    assert cut.blocks[0].text.startswith("### ❌")
    assert "<@111>" in cut.blocks[1].text
