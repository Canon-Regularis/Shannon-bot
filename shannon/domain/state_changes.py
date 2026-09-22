"""Which of the three state moves a delivery is, for the two actions that carry one.

Whether a closed pull request was merged is decided in `github/mapping.py`, which reads both
`merged` and `merged_at` because GitHub sends it either way round depending on the endpoint. The
action is a parameter rather than the copy the snapshot carries, because that copy is optional:
a sync driven by a command or the board has no action. Nothing here decides what to announce.
"""

from __future__ import annotations

from shannon.domain.enums import StateChange
from shannon.domain.models import TrackedSnapshot

# The two actions that are a state move. Every other action carries the item's state as well, so
# an item that is closed on an `edited` delivery was closed already and something else changed;
# announcing on those would say an item closed every time anybody touched a finished one.
STATE_ACTIONS = frozenset({"closed", "reopened"})


def state_change_of(action: str, snapshot: TrackedSnapshot) -> StateChange | None:
    """What this delivery did to the item's state, or None where it did nothing to it.

    Merging is read off `display_state` rather than off the action, because GitHub has no
    `merged` action: a merged pull request arrives as `closed` with a flag beside it.
    """
    if action not in STATE_ACTIONS:
        return None
    if action == "reopened":
        return StateChange.REOPENED
    return StateChange.MERGED if snapshot.display_state == "merged" else StateChange.CLOSED
