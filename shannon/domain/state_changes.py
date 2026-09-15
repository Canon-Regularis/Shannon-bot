"""Which of the three state moves a delivery is, for the two actions that carry one.

The action, and the snapshot for the one thing the action does not say. Whether a closed pull
request was merged is already decided in `github/mapping.py`, which reads `merged` and
`merged_at` because GitHub sends it either way round depending on the endpoint. Reading the raw
payload a second time here would put that rule in two places, and the two would not stay in step.

The action is a parameter rather than the copy the snapshot carries, because that copy is
optional: a sync driven by a command or the board has no action at all, and this is only ever
asked about a delivery, which always does.

Nothing here decides whether to announce anything. It says what the delivery did, and the
announcer decides whether the item still agrees with it.
"""

from __future__ import annotations

from shannon.domain.enums import StateChange
from shannon.domain.models import TrackedSnapshot

# The two actions that are a state move. Every other action carries the item's state as well,
# and an item that is closed on an `edited` delivery has not just closed: it was closed already,
# and something else about it changed. Announcing on those would say an item closed every time
# anybody touched a finished one.
STATE_ACTIONS = frozenset({"closed", "reopened"})


def state_change_of(action: str, snapshot: TrackedSnapshot) -> StateChange | None:
    """What this delivery did to the item's state, or None where it did nothing to it.

    Merging is read off `display_state` rather than off the action, because GitHub has no
    `merged` action: a merged pull request arrives as `closed` with a flag beside it, and the
    flag is what separates work finished from work abandoned. Those are different enough to a
    reader that telling them apart is the point of having a third answer at all.
    """
    if action not in STATE_ACTIONS:
        return None
    if action == "reopened":
        return StateChange.REOPENED
    return StateChange.MERGED if snapshot.display_state == "merged" else StateChange.CLOSED
