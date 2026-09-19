"""How a set of CI jobs is judged. Issue #112.

Tested here rather than only through the service, because the service asks these in an order that
hides one of them: it refuses a suite where nothing ran before it ever asks whether the suite
passed, so the "at least one success" half of `passed` is unreachable from that direction. It is
still the property the reviewers' ping hangs off, and a property nothing can reach is a property
nothing is holding.
"""

from __future__ import annotations

import pytest

from shannon.domain.models import BROKEN, SUCCEEDED, CheckReport, CheckRun

pytestmark = pytest.mark.unit


def run(number: int = 1, conclusion: str = "success") -> CheckRun:
    return CheckRun(
        check_run_id=number,
        name=f"Job {number}",
        status="completed",
        conclusion=conclusion,
        html_url=f"https://example.invalid/{number}",
    )


def report(*runs: CheckRun) -> CheckReport:
    return CheckReport(sha="c" * 40, runs=runs)


class TestTheThreeBuckets:
    def test_a_success_succeeded(self) -> None:
        assert [job.check_run_id for job in report(run()).succeeded] == [1]

    @pytest.mark.parametrize("conclusion", sorted(BROKEN))
    def test_what_counts_as_broken(self, conclusion: str) -> None:
        assert report(run(1, conclusion)).broken

    @pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "neutral", "stale", ""])
    def test_what_counts_as_neither(self, conclusion: str) -> None:
        """`stale` means GitHub gave up on the run, which says nothing about the code. An empty
        conclusion is a run GitHub did not describe, and guessing at it is the one thing worse
        than counting it separately."""
        judged = report(run(1, conclusion))

        assert judged.other
        assert not judged.broken
        assert not judged.succeeded

    def test_every_job_lands_in_exactly_one_bucket(self) -> None:
        mixed = report(run(1), run(2, "failure"), run(3, "skipped"), run(4, "timed_out"))

        assert len(mixed.succeeded) + len(mixed.broken) + len(mixed.other) == mixed.total == 4

    def test_the_two_sets_do_not_overlap(self) -> None:
        assert not SUCCEEDED & BROKEN


class TestWhetherItPassed:
    def test_everything_worked(self) -> None:
        assert report(run(1), run(2)).passed is True

    def test_a_job_that_did_not_run_does_not_stop_it_passing(self) -> None:
        """The whole reason there are three buckets. `Publish` is skipped on every pull request
        here, and a stricter reading would mean the reviewers were never told anything."""
        assert report(run(1), run(2, "skipped")).passed is True

    def test_one_broken_job_is_enough_to_fail(self) -> None:
        assert report(run(1), run(2, "failure")).passed is False

    def test_a_suite_where_nothing_ran_at_all_did_not_pass(self) -> None:
        """The half the service can never reach, because it refuses this case earlier. Without it
        a docs-only push through a path filter would read as a green build and ring the
        reviewers about a commit nothing was run against."""
        assert report(run(1, "skipped"), run(2, "cancelled")).passed is False

    def test_and_it_is_not_worth_saying_anything_about(self) -> None:
        assert report(run(1, "skipped")).worth_saying is False

    def test_anything_that_ran_is_worth_saying(self) -> None:
        assert report(run(1), run(2, "skipped")).worth_saying is True
        assert report(run(1, "failure"), run(2, "skipped")).worth_saying is True


class TestTheClaimKey:
    def test_it_names_the_count_and_the_largest_id(self) -> None:
        assert report(run(4), run(9)).note_key == "checks:2:9"

    def test_reading_the_same_set_twice_gives_the_same_key(self) -> None:
        """What makes a retried delivery say nothing the second time."""
        assert report(run(4), run(9)).note_key == report(run(9), run(4)).note_key

    def test_a_re_run_rotates_the_ids_and_so_the_key(self) -> None:
        assert report(run(4), run(9)).note_key != report(run(40), run(90)).note_key

    def test_a_provider_whose_older_runs_finish_later_still_moves_the_key(self) -> None:
        """The case the count is there for. Run ids are handed out at creation, so a second
        provider created earlier leaves the largest id exactly where it was, and a key built on
        that alone would find the claim taken and say nothing about six more jobs."""
        first = report(run(4), run(9))
        later = report(run(1), run(2), run(4), run(9))

        assert later.note_key != first.note_key
        assert later.note_key.endswith(":9"), "the largest id really is unchanged"

    def test_it_fits_the_column_that_holds_it(self) -> None:
        """`mirrored_notes.note_key` is `String(64)`, and a key that overflowed would raise on the
        insert rather than being truncated."""
        huge = report(*(run(10**12 + n) for n in range(500)))

        assert len(huge.note_key) <= 64
