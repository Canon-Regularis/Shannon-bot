"""Which GitHub label carries a status or a priority.

The repository is where these live: the requirements say the statuses exist as tags on it, and
priority has been read off labels since MVP 2. Writing them back means agreeing on one spelling
to write and accepting every spelling `parse_priority` already reads, or an item labelled
`urgent` would come back HIGH, be set to HIGH, and end up carrying two priority labels that
disagree the moment somebody edits one.

Nothing here talks to GitHub. It decides which labels an item should lose and which it should
gain, and the client does as it is told.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shannon.domain.enums import Priority, Status
from shannon.domain.priority import parse_priority

# What gets written. Statuses are the enum values as the requirements spell them; priorities are
# the bare word, which `parse_priority` reads back as itself.
STATUS_LABELS: dict[Status, str] = {status: status.value for status in Status}
# UNSET is deliberately absent, because it is the absence of a priority rather than one of them
# and there is no label that says so. Nothing reaches here with it: the three priority commands
# are the only callers and each carries its own value. A fourth that meant "clear the priority"
# would want removals with no addition, which `LabelChange` can already express, rather than a
# row here.
PRIORITY_LABELS: dict[Priority, str] = {
    Priority.HIGH: "HIGH",
    Priority.MEDIUM: "MEDIUM",
    Priority.LOW: "LOW",
}

_STATUS_BY_LABEL = {label.casefold(): status for status, label in STATUS_LABELS.items()}


@dataclass(frozen=True, slots=True)
class LabelChange:
    """The labels to take off an item and the one to put on.

    Both halves are needed because a status and a priority are each single-valued. Adding
    without removing leaves an item reading BACKLOG and IN_REVIEW at once, and which one wins
    then depends on which reader you ask.
    """

    remove: tuple[str, ...]
    add: str

    @property
    def nothing_to_do(self) -> bool:
        return not self.remove and not self.add


def status_of(label_names: Iterable[str]) -> Status | None:
    """The status an item's labels say it has, or None if they say nothing.

    Only the exact written spellings count, unlike priority. A repository is free to have a
    label called `done` meaning something else entirely, and guessing at synonyms here would
    move items through a workflow nobody asked it to.
    """
    for name in label_names:
        status = _STATUS_BY_LABEL.get(name.strip().casefold())
        if status is not None:
            return status
    return None


def status_change(current: Iterable[str], wanted: Status) -> LabelChange:
    """Take off whatever status the item is carrying and put the wanted one on."""
    names = list(current)
    stale = tuple(
        name
        for name in names
        if _STATUS_BY_LABEL.get(name.strip().casefold()) not in (None, wanted)
    )
    already = status_of(names) is wanted
    return LabelChange(remove=stale, add="" if already else STATUS_LABELS[wanted])


def priority_change(current: Iterable[str], wanted: Priority) -> LabelChange:
    """The same for priority, against every spelling the parser accepts.

    Removing only the label this bot would have written is not enough. Priority has been read
    from whatever the repository already uses since MVP 2, so an item can be carrying `urgent`
    or `HIGH_PRIORITY`, and leaving one of those behind means the item still reads HIGH after
    being set to LOW. Whatever `parse_priority` reads is what comes off, which is the only rule
    that cannot leave the two disagreeing.

    Case-folded, the way `status_change` above compares. GitHub's own stock labels are lowercase
    and a repository that spells this one `high` was carrying a label this read as stale purely
    for its case: it came off and `HIGH` went on, and since GitHub matches a label name without
    regard to case, the add re-attached the very label just removed. So the item still read
    `high`, the next run of the command did the same two writes again, and every one of them
    answered "is now HIGH priority" for an item that had been HIGH all along.
    """
    names = list(current)
    stale = tuple(
        name
        for name in names
        if _is_priority(name) and name.strip().casefold() != wanted.value.casefold()
    )
    already = parse_priority(names) is wanted and not stale
    return LabelChange(remove=stale, add="" if already else PRIORITY_LABELS[wanted])


def reserved_as(name: str) -> Status | Priority | None:
    """What this name already means to this bot, or None if it is an ordinary label.

    Asked before a label is set by hand. Both halves of the workflow live as labels on the
    repository, so a command that sets any label can reach them, and reaching them is what makes
    the block contradict itself: nothing on the webhook path reads a status back onto the stored
    column, so the item would show one status in its block and carry the label of another.

    Priority is the sharper half and fails the opposite way round. `parse_priority` DOES feed the
    stored column on every sync, so writing `critical` by hand changes an item's priority from a
    command that never mentioned priority.

    Built out of the two classifiers already in this module, so the reserved set is exactly what
    the rest of it reads and cannot drift from it. That is wider than the eight names written
    here: every spelling `parse_priority` accepts is reserved, because every one of them is read.
    """
    status = status_of([name])
    if status is not None:
        return status
    priority = parse_priority([name])
    return priority if priority is not Priority.UNSET else None


def label_change(current: Iterable[str], name: str, *, adding: bool) -> LabelChange:
    """Put one ordinary label on an item, or take it off.

    Nothing comes off to make room, unlike the two changes above. A status and a priority are
    each single-valued, which is the whole reason those two remove before they add; an ordinary
    label is not, and an item may carry as many as somebody finds useful.

    A label the item already holds is no change at all, compared case-folded because GitHub
    matches a label name without regard to case. Left uncompared, adding `Bug` to an item holding
    `bug` writes, re-attaches the label GitHub already had, and answers as though something
    happened, which is the loop `priority_change` above describes.
    """
    wanted = name.strip().casefold()
    on_it = any(held.strip().casefold() == wanted for held in current)
    if adding:
        return LabelChange(remove=(), add="" if on_it else name)
    return LabelChange(remove=(name,) if on_it else (), add="")


def _is_priority(name: str) -> bool:
    return parse_priority([name]) is not Priority.UNSET
