"""Which GitHub label carries a status or a priority, and how each one looks.

Statuses and priorities both live as labels on the repository. Writing one back means agreeing on
a single spelling to write while accepting every spelling `parse_priority` reads, or an item
labelled `urgent` comes back HIGH, is set to HIGH, and carries two priority labels that disagree.

Issue #246 gave them names that say what they are and colours that tell them apart. A priority
is `priority: High`, `priority: Medium` or `priority: Low`, in red, amber and yellow. A status is
`status: ` and the board column it is in, spelled as the board spells it and coloured as the
board colours it, so a column this bot has no word for - `Ready To Merge` - is a status label
like any other. The enum values are only what the database stores, and nothing here writes them
any more: the labels this bot used to write (`HIGH`, `IN_REVIEW` and the rest) are still read, so
an item carrying one is cleared properly, and each is renamed in place to its new name the first
time this bot writes that one in a repository - except where the repository has the new name
already, which `services/label_definitions.py` explains, and where the board has two columns for
one status, which `status_label_on` explains. Every label starting `status:` is a status label,
including ones a repository made itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from shannon.domain.board import landing_column, named_column, status_from_column
from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.priority import STATUS_PREFIX, parse_priority
from shannon.github.projects import BoardColumn

# GitHub's ceiling on a label name. A board column may be longer: the column this bot stores is a
# hundred and twenty-eight wide.
LONGEST_LABEL = 50


@dataclass(frozen=True, slots=True)
class LabelDefinition:
    """A label this bot writes: its name, its colour, and the older names it takes over from.

    `replaces` is what makes the rename in place work. The first of those spellings a repository
    still has is renamed to `name`, which moves it on every item carrying it at once, so an issue
    labelled `HIGH` last week reads `priority: High` without anybody touching it - where the
    repository has no `priority: High` yet, since GitHub will not rename onto a name it has.
    """

    name: str
    # Six hex digits, no `#`, which is how GitHub takes and gives a label's colour.
    colour: str
    replaces: tuple[str, ...] = ()


PRIORITY_LABELS: dict[Priority, LabelDefinition] = {
    Priority.HIGH: LabelDefinition("priority: High", "d73a4a", ("HIGH",)),
    Priority.MEDIUM: LabelDefinition("priority: Medium", "f59e0b", ("MEDIUM",)),
    Priority.LOW: LabelDefinition("priority: Low", "fde047", ("LOW",)),
}

# What a status label used to be: the enum's own spelling. Read and never written.
# `READY_FOR_MERGE` is not one: it stopped being a status before #246 and is an ordinary label now.
LEGACY_STATUS_LABELS: dict[str, Status] = {status.value.casefold(): status for status in Status}

# A board column's colour, as GitHub's project boards name them, as a label colour.
BOARD_COLOURS: dict[str, str] = {
    "GRAY": "8b949e",
    "BLUE": "0969da",
    "GREEN": "1a7f37",
    "YELLOW": "d4a72c",
    "ORANGE": "fb8500",
    "RED": "cf222e",
    "PINK": "e85aad",
    "PURPLE": "8250df",
}

# The colour of a status label where no board column gives one: a repository mirroring no board,
# or a board whose column carries no colour.
STATUS_COLOURS: dict[Status, str] = {
    Status.BACKLOG: BOARD_COLOURS["GRAY"],
    Status.NOT_REVIEWED: BOARD_COLOURS["BLUE"],
    Status.IN_REVIEW: BOARD_COLOURS["PURPLE"],
    Status.DONE: BOARD_COLOURS["GREEN"],
}
# A column this bot has no status for, on a board that gave it no colour either.
UNKNOWN_STATUS_COLOUR = BOARD_COLOURS["GRAY"]


@dataclass(frozen=True, slots=True)
class LabelChange:
    """The labels to take off an item and the one to put on.

    Both halves are needed because a status and a priority are each single-valued: adding without
    removing leaves an item reading BACKLOG and IN_REVIEW at once. `define` is the label being put
    on, where it is one of this bot's own, so the repository has it in the right colour first.
    """

    remove: tuple[str, ...]
    add: str
    define: LabelDefinition | None = None

    @property
    def nothing_to_do(self) -> bool:
        return not self.remove and not self.add


def status_label(column: str, *, board_colour: str | None = None) -> LabelDefinition:
    """The label for a status column: `status: <column>`, in the column's own colour.

    The old enum spelling it replaces is the one for the status this column reads as, and only
    that: a column this bot has no status for takes over from nothing, because there is no way to
    tell which of the old labels meant it.
    """
    name = f"{STATUS_PREFIX} {column.strip()}"
    status = status_from_column(column)
    colour = BOARD_COLOURS.get((board_colour or "").upper()) or (
        STATUS_COLOURS[status] if status is not None else UNKNOWN_STATUS_COLOUR
    )
    replaces = tuple(
        old.upper() for old, reads_as in LEGACY_STATUS_LABELS.items() if reads_as is status
    )
    return LabelDefinition(name, colour, replaces)


@dataclass(frozen=True, slots=True)
class StatusLabel:
    """The label a status move writes, and whether its name and colour are known to be right.

    Not settled where the board would not read, or named a column it no longer has: the label is
    then the best name to hand, and making the repository hold it in a colour - which renames a
    label spelled differently on every item carrying it - would be acting on a guess.
    """

    definition: LabelDefinition
    settled: bool


def status_label_on(
    board: Sequence[BoardColumn] | None, status: Status, named: str = ""
) -> StatusLabel:
    """The status label for a move to this status or this column, on a board with these columns.

    `board` is None where the board would not read, and empty where there is no board at all.

    On a board, the column the card lands in - `landing_column`, the same resolver the card write
    uses, so the label and the card cannot name two different columns - spelled and coloured as
    the board has it. With no board, or a board with no column for the status, one of this bot's
    own four names. A column this bot has no status for that the board does not have, or any
    column while the board will not read, keeps the name it was given, unsettled.

    The old label for a status is taken over only where nothing else could claim it. A board
    with two columns for one status - GitHub's own template has `In progress` and `In review` -
    would otherwise hand `IN_REVIEW` to whichever was written first, renaming it on every item
    carrying it, including the ones whose cards are in the other column. There the old label is
    left, and comes off each item as it is next moved.
    """
    named = named.strip()
    if board is None:
        return StatusLabel(status_label(named or own_column(status)), settled=False)
    names = [column.name for column in board]
    if named and status_from_column(named) is None and named_column(names, named) is None:
        return StatusLabel(status_label(named), settled=False)
    at = landing_column(names, status, named)
    if at is None:
        return StatusLabel(status_label(own_column(status)), settled=True)
    column = board[at]
    definition = status_label(column.name, board_colour=column.colour)
    reads_as = status_from_column(column.name)
    if sum(1 for name in names if status_from_column(name) is reads_as) > 1:
        definition = replace(definition, replaces=())
    return StatusLabel(definition, settled=True)


def own_column(status: Status) -> str:
    """The column this bot names a status by where nothing else names one."""
    return spoken(status)


def is_status_label(name: str) -> bool:
    """Whether a label carries a status, in today's spelling or the one before #246."""
    folded = name.strip().casefold()
    return folded.startswith(STATUS_PREFIX) or folded in LEGACY_STATUS_LABELS


def status_of(label_names: Iterable[str]) -> Status | None:
    """The status an item's labels say it has, or None if they say nothing this bot can read.

    Only status labels count, unlike priority: a repository's own `shipped` label may mean
    something else, and guessing at synonyms would move items through a workflow nobody asked
    for. A status label for a column this bot has no status for says nothing here, though it is
    still a status label - `is_status_label` - and comes off when another is put on.
    """
    for name in label_names:
        status = _status_named(name)
        if status is not None:
            return status
    return None


def status_change(current: Iterable[str], wanted: LabelDefinition) -> LabelChange:
    """Take off whatever status label the item is carrying and put the wanted one on.

    Compared case-folded, because GitHub matches a label name without regard to case.
    """
    names = list(current)
    target = wanted.name.casefold()
    stale = tuple(
        name for name in names if is_status_label(name) and name.strip().casefold() != target
    )
    on_it = any(name.strip().casefold() == target for name in names)
    return LabelChange(
        remove=stale, add="" if on_it else wanted.name, define=None if on_it else wanted
    )


def priority_change(current: Iterable[str], wanted: Priority) -> LabelChange:
    """The same for priority, against every spelling the parser accepts.

    Priority is read from whatever labels the repository already uses, so an item can be carrying
    `urgent` or `HIGH_PRIORITY`, and leaving one behind means it still reads HIGH after being set
    to LOW. The comparison is against the label this writes, case-folded because GitHub matches a
    label name without regard to case - against the enum's spelling it was right only while the
    two were the same word, and with `priority: High` every run would have taken the label off and
    put it straight back.
    """
    names = list(current)
    definition = PRIORITY_LABELS[wanted]
    target = definition.name.casefold()
    stale = tuple(
        name for name in names if _is_priority(name) and name.strip().casefold() != target
    )
    on_it = any(name.strip().casefold() == target for name in names)
    return LabelChange(
        remove=stale, add="" if on_it else definition.name, define=None if on_it else definition
    )


def reserved_as(name: str) -> Priority | str | None:
    """What this name already means to this bot, or None if it is an ordinary label.

    A priority, or the status column as somebody would pick it. Asked before a label is set by
    hand. A hand-set status leaves an item showing one status in its block and carrying the label
    of another; `parse_priority` feeds the stored priority on every sync, so a hand-set
    `critical` changes an item's priority from a command that never mentioned priority. Reserved
    is therefore every spelling those two classifiers read.
    """
    if is_status_label(name):
        folded = name.strip().casefold()
        legacy = LEGACY_STATUS_LABELS.get(folded)
        if legacy is not None:
            return own_column(legacy)
        # A bare `status:` names no column, and is still not a label to set by hand.
        return name.strip()[len(STATUS_PREFIX) :].strip() or "a column"
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


def _status_named(name: str) -> Status | None:
    folded = name.strip().casefold()
    if folded.startswith(STATUS_PREFIX):
        return status_from_column(name.strip()[len(STATUS_PREFIX) :])
    return LEGACY_STATUS_LABELS.get(folded)


def _is_priority(name: str) -> bool:
    return parse_priority([name]) is not Priority.UNSET
