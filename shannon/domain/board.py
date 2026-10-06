"""Reading a project board's column back as one of our statuses, and what order they come in.

GitHub's default board template spells them `Todo`, `In Progress`, `Done`, so those are accepted
beside our own four. More forgiving than `labels.status_of`, where a repository may have a `done`
meaning something else; a board's Status column is a small closed set chosen for this workflow.

The order is here too, because it is the only rule a board can express. Projects v2 has no notion
of an allowed transition - `ProjectV2Workflow` carries a name and an on/off flag and no condition
of any kind, its only mutation is a delete, and rulesets target branches rather than projects. So
the one thing on a board that says anything about which move comes after which is the order its
Status options are returned in, which is left to right as somebody arranged them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from shannon.domain.enums import Status

_SEPARATORS = re.compile(r"[\s_\-:/]+")

# Normalised keys: lowercased, with runs of punctuation and space collapsed to one space.
_COLUMNS: dict[str, Status] = {
    "backlog": Status.BACKLOG,
    "icebox": Status.BACKLOG,
    "on hold": Status.BACKLOG,
    "blocked": Status.BACKLOG,
    "not reviewed": Status.NOT_REVIEWED,
    "todo": Status.NOT_REVIEWED,
    "to do": Status.NOT_REVIEWED,
    "new": Status.NOT_REVIEWED,
    "open": Status.NOT_REVIEWED,
    "ready": Status.NOT_REVIEWED,
    "in review": Status.IN_REVIEW,
    "in progress": Status.IN_REVIEW,
    "doing": Status.IN_REVIEW,
    "started": Status.IN_REVIEW,
    "under review": Status.IN_REVIEW,
    "done": Status.DONE,
    "closed": Status.DONE,
    "complete": Status.DONE,
    "completed": Status.DONE,
    "shipped": Status.DONE,
}


def columns_for(status: Status) -> tuple[str, ...]:
    """Every column name this bot reads as one status, in the table's own order.

    Derived rather than written out beside it: a word added to the table and not to the
    sentence would be one this bot accepts and never offers, which is the whole failure
    mode of a second hand-kept list.

    Normalised, because that is how the table is keyed - a board spelling one `In Progress`
    is told about `in progress` and is not misled by the difference, since the lookup
    normalises too.

    Complete for BOTH passes of the picker that writes a column, and only because every
    status's own spoken form is itself a key here mapping to that status. A test holds
    that, because it is otherwise an accident: break it and this would offer a list
    missing the one name the exact-match pass would have taken.
    """
    return tuple(column for column, reads_as in _COLUMNS.items() if reads_as is status)


def normalise(column: str) -> str:
    return _SEPARATORS.sub(" ", column.strip().lower()).strip()


def status_from_column(column: str | None) -> Status | None:
    """The status a board column stands for, or None for one nobody has taught us.

    None rather than a default: guessing NOT_REVIEWED would move real work backwards on every poll.
    """
    if not column:
        return None
    return _COLUMNS.get(normalise(column))


def must_pass_through(columns: Sequence[str], *, frm: str | None, to: str) -> tuple[str, ...]:
    """The columns a move has to go through first, empty where the board allows it directly.

    Forward one column at a time, backwards as far as you like. The asymmetry is the point rather
    than an oversight: sending work back for rework is ordinary, and declaring it finished early is
    the thing worth stopping. It replaces a rule that used to be written out in Python - a pull
    request had to be `Ready for merge` before it could be `Done` - with the same requirement
    expressed in whatever columns a board actually has. On GitHub's default template,
    `In review -> Done` is one step and `Ready -> Done` skips two, so the protection survives
    without this file holding an opinion about what the columns are called.

    Every column counts as a step, and that is only true because the picker offers every column.
    It was not always. While `/status` took a fixed list of four names, nothing could write to a
    column called `In progress` - the exact-name pass takes `In review` for IN_REVIEW - so counting
    it as a step demanded a move nobody could make, and left a card in `Ready` with no way forward
    at all. That needed a filter over which columns were writable. The picker is the board's own
    columns now, so a step this refuses is always one somebody can take, and the filter is gone
    rather than merely unused.

    On GitHub's default template `Ready -> Done` still skips `In review` and is still refused, which
    is the retired READY_FOR_MERGE gate read off the board instead of named in Python.

    Empty for anything it cannot reason about: a column not on this board, a board whose options
    could not be read, or an item whose column nobody has recorded yet. A rule derived from a list
    this bot did not write has to fail open, or renaming one column would refuse every move on the
    board until somebody renamed it back.

    The names returned are the board's own spelling, not the normalised form, because they go
    straight into a sentence somebody reads.
    """
    order: dict[str, int] = {}
    for at, column in enumerate(columns):
        # First wins, so two columns that normalise alike cannot silently reorder the board.
        order.setdefault(normalise(column), at)

    here = order.get(normalise(frm)) if frm else None
    there = order.get(normalise(to))
    if here is None or there is None or there <= here:
        return ()

    return tuple(columns[at] for at in range(here + 1, there))


def board_owner(*, project_owner: str | None, repo_name: str) -> str:
    """Which GitHub account a repository's board belongs to.

    The one place `project_owner or <the repository's own owner>` is written. It was written in four
    and the fallback is not arbitrary in any of them: a board number is a sequence GitHub keeps per
    ACCOUNT, so the pair addresses a board and neither half does alone, and an EMPTY owner used to
    send a board write out with no credential at all - GitHub answered 401, and the poller read that
    as permanent and wrote the card off for good.

    Null means the board belongs to the repository's own owner, which is what every row written
    before the column existed says and needs no backfill. It follows the repository through a
    rename of the repository, and of the account where its id proves it the same one; a move to
    another account, or one nothing can tell from it, writes the old owner in first, because the
    same number under the new owner is a different board.

    Plain values rather than a `Repository`, so `domain` stays free of the ORM - the argument
    `shannon/services/sync/draft_cards.py` already makes for `snapshot_of`. Nothing here imports
    upward.
    """
    return project_owner or repo_name.partition("/")[0]


@dataclass(frozen=True, slots=True)
class ChosenBoard:
    """A board somebody named, and the owner where they named one.

    The owner is empty for a bare number, which is every entry the picker offers: those are
    already listed under an owner, so repeating it would be the command telling the service
    something the service just told it. Empty rather than a guess, because `board_owner` above is
    the one place an absent owner is resolved.

    Here rather than beside the command that parses it, because since issue #201 the choice
    outlives the command. A board link that has to send somebody to GitHub first writes the board
    they chose onto the pending verification row, and the callback reads it back from there - so
    `db` and `services` both hold one, and neither may import from `commands`.
    """

    number: int
    owner: str = ""
