"""Which GitHub label carries a status or a priority.

Statuses and priorities both live as labels on the repository. Writing one back means agreeing on
a single spelling to write while accepting every spelling `parse_priority` reads, or an item
labelled `urgent` comes back HIGH, is set to HIGH, and carries two priority labels that disagree.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shannon.domain.enums import Priority, Status
from shannon.domain.priority import parse_priority

# What gets written: the enum's own spelling for a status, the bare word for a priority, which
# `parse_priority` reads back as itself.
STATUS_LABELS: dict[Status, str] = {status: status.value for status in Status}
# UNSET is absent: it is the absence of a priority rather than one of them, and no label says
# so. The three priority commands are the only callers and each carries a real value, so nothing
# looks it up here.
PRIORITY_LABELS: dict[Priority, str] = {
    Priority.HIGH: "HIGH",
    Priority.MEDIUM: "MEDIUM",
    Priority.LOW: "LOW",
}

_STATUS_BY_LABEL = {label.casefold(): status for status, label in STATUS_LABELS.items()}


@dataclass(frozen=True, slots=True)
class LabelChange:
    """The labels to take off an item and the one to put on.

    Both halves are needed because a status and a priority are each single-valued: adding without
    removing leaves an item reading BACKLOG and IN_REVIEW at once.
    """

    remove: tuple[str, ...]
    add: str

    @property
    def nothing_to_do(self) -> bool:
        return not self.remove and not self.add


def status_of(label_names: Iterable[str]) -> Status | None:
    """The status an item's labels say it has, or None if they say nothing.

    Only the exact spellings count, unlike priority: a repository's own `done` label may mean
    something else, and guessing at synonyms would move items through a workflow nobody asked for.
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

    Priority is read from whatever labels the repository already uses, so an item can be carrying
    `urgent` or `HIGH_PRIORITY`, and leaving one behind means it still reads HIGH after being set
    to LOW. The comparison is case-folded because GitHub matches a label name without regard to
    case: against a repository spelling this `high`, the removal and the add hit the same label,
    so every run wrote twice and still answered that the priority had changed.
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

    Asked before a label is set by hand. Nothing on the webhook path reads a status back onto the
    stored column, so a hand-set status leaves an item showing one status in its block and
    carrying the label of another; `parse_priority` does feed that column on every sync, so a
    hand-set `critical` changes an item's priority from a command that never mentioned priority.
    Reserved is therefore every spelling those two classifiers read, not just the eight names here.
    """
    status = status_of([name])
    if status is not None:
        return status
    priority = parse_priority([name])
    return priority if priority is not Priority.UNSET else None


def label_change(current: Iterable[str], name: str, *, adding: bool) -> LabelChange:
    """Put one ordinary label on an item, or take it off.

    Nothing comes off to make room: unlike a status or a priority, an ordinary label is not
    single-valued. A label the item already holds is no change, compared case-folded for the
    reason `priority_change` gives.
    """
    wanted = name.strip().casefold()
    on_it = any(held.strip().casefold() == wanted for held in current)
    if adding:
        return LabelChange(remove=(), add="" if on_it else name)
    return LabelChange(remove=(name,) if on_it else (), add="")


def _is_priority(name: str) -> bool:
    return parse_priority([name]) is not Priority.UNSET
