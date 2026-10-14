"""Which kinds of line a thread is told, and what it is told by default. Issue #163.

Besides its block, which is edited in place and always kept current, an item's thread gets a new
line for things that happen to the item: a tag moved, a commit pushed, CI finished. Most of those
ring nobody, and in a busy repository they bury the conversation the thread is there for. So each
kind of line can be turned on or off for one thread, with `/track`, and starts at a default.

What is written down is only what somebody chose. A kind nobody chose follows `DEFAULTS` as they
stand when the line is decided, so a default changed in a later release reaches every thread that
never said otherwise, and a thread that did say keeps its answer.

Not every line is a kind. A thread is always told when it has been moved to another channel, when
logging to GitHub starts or stops or gives up, when a reminder somebody set there goes off, and
when somebody is asked to link their GitHub account. Each of those answers a person who did
something in Discord, and none of them is a change to the item that the thread could do without.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from shannon.domain.enums import ObjectType
from shannon.domain.json import is_json_object


class TrackedEvent(StrEnum):
    """A kind of line a thread can be told.

    The values are a stored format as well as what Discord offers as choices: they are the keys of
    `tracked_items.tracking`, so renaming one forgets every choice made under the old name.
    """

    COMMENTS = "comments"
    LABELS = "labels"
    COMMITS = "commits"
    CHECKS = "checks"
    APPROVALS = "approvals"
    DRAFT = "draft"
    PEOPLE = "people"
    STATE = "state"


# What a thread is told when nobody there has said. Off for the two that ring nobody and arrive by
# the dozen - every tag moved and every commit pushed - and on for everything else: a line that
# rings somebody is a line somebody is owed, and the closing header says what became of the thread.
DEFAULTS: dict[TrackedEvent, bool] = {
    TrackedEvent.COMMENTS: True,
    TrackedEvent.LABELS: False,
    TrackedEvent.COMMITS: False,
    TrackedEvent.CHECKS: True,
    TrackedEvent.APPROVALS: True,
    TrackedEvent.DRAFT: True,
    TrackedEvent.PEOPLE: True,
    TrackedEvent.STATE: True,
}

# Which kinds can happen to each kind of item at all, in the order they are listed. An issue has
# no commits, CI, reviews to approve or draft switch; a board card has neither comments nor people,
# only fields that move and a card that is archived, restored, deleted or converted.
HAPPENS_TO: dict[ObjectType, tuple[TrackedEvent, ...]] = {
    ObjectType.PR: tuple(TrackedEvent),
    ObjectType.ISSUE: (
        TrackedEvent.COMMENTS,
        TrackedEvent.LABELS,
        TrackedEvent.PEOPLE,
        TrackedEvent.STATE,
    ),
    ObjectType.TICKET: (TrackedEvent.LABELS, TrackedEvent.STATE),
}


@dataclass(frozen=True, slots=True)
class Tracking:
    """What one thread chose, and what follows from it for every kind.

    Holds only the kinds somebody turned on or off; everything else falls back to `DEFAULTS` when
    asked, never earlier, which is what lets a changed default reach a thread that never chose.
    Hashable, so `Tracking()` - nobody chose anything - can be a dataclass default.
    """

    on: frozenset[TrackedEvent] = frozenset()
    off: frozenset[TrackedEvent] = frozenset()

    @classmethod
    def of(cls, stored: object) -> Tracking:
        """Read what was written down, ignoring anything this release does not know.

        A key that is not a kind, or a value that is not a real `true` or `false`, is skipped
        rather than guessed at: a key from a later release survives a rollback this way, and a
        hand-edited row cannot turn a line on by being merely truthy.
        """
        if not is_json_object(stored):
            return cls()
        on: set[TrackedEvent] = set()
        off: set[TrackedEvent] = set()
        for event in TrackedEvent:
            chosen = stored.get(event.value)
            if chosen is True:
                on.add(event)
            elif chosen is False:
                off.add(event)
        return cls(on=frozenset(on), off=frozenset(off))

    def wants(self, event: TrackedEvent) -> bool:
        """Whether this thread is told about `event`: what it chose, or else the default."""
        if event in self.on:
            return True
        if event in self.off:
            return False
        return DEFAULTS[event]
