"""One run at a time per key, and a wait after each one finishes. Issue #185."""

from __future__ import annotations

from shannon.commands._cooldowns import Cooldown, NotYet, seconds_in_words

SPACING = 60.0


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def a_cooldown(clock: Clock) -> Cooldown:
    return Cooldown(SPACING, clock=clock)


class TestTakingTurns:
    def test_the_first_run_may_start(self) -> None:
        assert a_cooldown(Clock()).begin(1) is None

    def test_a_second_run_while_one_is_under_way_is_told_so(self) -> None:
        cooldown = a_cooldown(Clock())
        cooldown.begin(1)

        assert cooldown.begin(1) == NotYet(running=True, seconds=0)

    def test_another_key_is_not_held_up(self) -> None:
        cooldown = a_cooldown(Clock())
        cooldown.begin(1)

        assert cooldown.begin(2) is None

    def test_a_refusal_does_not_end_the_run_under_way(self) -> None:
        """The caller is told not to finish what it did not begin, and nor does the refusal."""
        cooldown = a_cooldown(Clock())
        cooldown.begin(1)
        cooldown.begin(1)

        assert cooldown.begin(1) == NotYet(running=True)


class TestTheWaitAfter:
    def test_it_counts_from_the_finish_rather_than_the_start(self) -> None:
        """A run of five minutes followed by ten seconds leaves fifty to wait, not none."""
        clock = Clock()
        cooldown = a_cooldown(clock)
        cooldown.begin(1)
        clock.now = 300.0
        cooldown.finish(1)
        clock.now = 310.0

        assert cooldown.begin(1) == NotYet(running=False, seconds=50)

    def test_the_time_left_is_rounded_up(self) -> None:
        """So the wait a reply promises is never too short to be true."""
        clock = Clock()
        cooldown = a_cooldown(clock)
        cooldown.begin(1)
        cooldown.finish(1)

        clock.now = 0.8
        assert cooldown.begin(1) == NotYet(running=False, seconds=60)
        clock.now = 59.8
        assert cooldown.begin(1) == NotYet(running=False, seconds=1)

    def test_at_exactly_the_spacing_it_may_start(self) -> None:
        clock = Clock()
        cooldown = a_cooldown(clock)
        cooldown.begin(1)
        cooldown.finish(1)
        clock.now = SPACING

        assert cooldown.begin(1) is None

    def test_asking_again_while_waiting_does_not_start_the_wait_over(self) -> None:
        clock = Clock()
        cooldown = a_cooldown(clock)
        cooldown.begin(1)
        cooldown.finish(1)
        clock.now = 30.0
        cooldown.begin(1)
        clock.now = 50.0

        assert cooldown.begin(1) == NotYet(running=False, seconds=10)

    def test_another_key_has_no_wait_of_its_own(self) -> None:
        cooldown = a_cooldown(Clock())
        cooldown.begin(1)
        cooldown.finish(1)

        assert cooldown.begin(2) is None

    def test_spent_waits_are_let_go_of(self) -> None:
        """A key used once must not be held for the life of the process."""
        clock = Clock()
        cooldown = a_cooldown(clock)
        for key in range(100):
            cooldown.begin(key)
            cooldown.finish(key)
        clock.now = SPACING

        cooldown.begin(999)

        assert cooldown._finished == {}


class TestTheWaitInWords:
    def test_one_second(self) -> None:
        assert seconds_in_words(1) == "1 second"

    def test_more_than_one(self) -> None:
        assert seconds_in_words(45) == "45 seconds"
