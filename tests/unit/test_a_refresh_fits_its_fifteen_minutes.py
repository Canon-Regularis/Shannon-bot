"""A run of `/refresh` finishes inside the fifteen minutes Discord gives a deferred command.

Issue #185 gave a run a second half: once its openings are done it redraws for up to
`REDRAW_SECONDS`. The two limits are set apart, and only their sum is what Discord holds the
command to, so the sum is held here where a change to either one meets it.
"""

from __future__ import annotations

from shannon.services.sync.one_at_a_time import LOCK_WAIT_SECONDS
from shannon.services.sync.refresh import MIRRORED_PER_RUN, REDRAW_SECONDS

# How long Discord keeps a deferred interaction's token taking a reply.
INTERACTION_TOKEN_SECONDS = 15 * 60


def test_the_worst_case_still_leaves_time_to_reply() -> None:
    """Every opening can wait out the whole item lock behind a delivery, and so can the last redraw
    started inside the redraw time. A fifth of the token is left for the Discord calls themselves
    and the reply, which is the one part a run that overran could not deliver."""
    worst = MIRRORED_PER_RUN * LOCK_WAIT_SECONDS + REDRAW_SECONDS + LOCK_WAIT_SECONDS

    assert worst <= 0.8 * INTERACTION_TOKEN_SECONDS
