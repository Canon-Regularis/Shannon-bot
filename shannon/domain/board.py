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

from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.priority import parse_priority

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


def named_column(columns: Sequence[str], wanted: str) -> int | None:
    """Where in these columns the one called exactly this is, however either side spells it.

    Both sides go through `normalise`, so a board saying `In Progress` and somebody typing
    `in-progress` mean the same column.
    """
    asked = normalise(wanted)
    return next((at for at, column in enumerate(columns) if normalise(column) == asked), None)


def reads_as(column: str, state: Status | Priority) -> bool:
    """Whether a column name is a SYNONYM for this state.

    Both tables are many-to-one and both are already the single source for their half:
    `status_from_column` for a column, `parse_priority` for a label. The second answers UNSET
    where the first answers None, so it is folded to None here the way `labels.reserved_as`
    already folds it.
    """
    if isinstance(state, Status):
        return status_from_column(column) is state
    found = parse_priority([column])
    return found is not Priority.UNSET and found is state


def column_for(columns: Sequence[str], state: Status | Priority) -> int | None:
    """Where the board's own column standing for this state is, or None where it has none.

    Two passes, and the first one is not an optimisation. The words a board may use are read
    through tables that are many-to-one on purpose - `In progress` and `In review` both mean
    IN_REVIEW, `urgent` and `critical` both mean HIGH - because a board using either is saying
    the same thing. That is right for READING a column back.

    Writing one, it is not enough. A board carrying BOTH columns is the ordinary case rather than
    a corner - GitHub's own default template ships Backlog, Ready, In progress, In review, Done -
    and picking whichever came first would move a card to `In progress` when somebody asked for
    `In review`. So a column called exactly what this bot calls the state wins over one that
    merely maps to it.

    The fallback keeps board order, which is left to right. It only decides between synonyms,
    where neither is the state's own name and there is nothing better to go on.
    """
    exact = named_column(columns, spoken(state))
    if exact is not None:
        return exact
    return next((at for at, column in enumerate(columns) if reads_as(column, state)), None)


def landing_column(columns: Sequence[str], state: Status | Priority, named: str = "") -> int | None:
    """Where a card asked to move to this state, or to this column, lands on these columns.

    The column somebody named, or the one the state maps to where they named none or named one
    this board does not have. One resolver for the card write, the rule that decides whether the
    write is allowed, and since issue #246 the status label, which names the column the card is
    in: three answers reached by different routes would refuse moves they then made, or label a
    card `status: In review` while it sits in `In progress`.

    A name that is not on this board falls back rather than failing. It arrives from a picker
    that had three seconds and may have shown nothing at all, so what reaches here may be typed
    prose or a column from the board this repository mirrored last week.
    """
    if named:
        found = named_column(columns, named)
        if found is not None:
            return found
    return column_for(columns, state)


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


# The page a draft card is linked to, because a draft has none of its own: the board's. Written by
# the board reader's `_board_url`, and read back here to say which board a ticket row came from.
_BOARD_PAGE = re.compile(
    r"https://github\.com/(?:users|orgs)/(?P<owner>[^/?#]+)/projects/(?P<number>[0-9]+)"
)


def is_board_page(url: str, *, owner: str, number: int) -> bool:
    """Whether a ticket row's link is this board's own page, owner and number both.

    Issue #198. A ticket row records its board twice - the number, and this page - and the number
    alone is not a board: GitHub keeps the sequence per account. A server that relinks to another
    owner's board of the same number still holds the old board's ticket rows, and a card missing
    from the new board's listing is asked about on the new board - where every one of the old
    cards answers that GitHub has no such card, and would be shut as deleted. The owner in the page
    is what tells the two boards apart.

    Owner compared without its case, as GitHub compares logins. The users-or-orgs half is not
    compared: an account is one or the other, so the owner already decides it. The page a card
    with an unreadable project link is given, `users/unknown`, never matches a real board, which
    errs the safe way - such a card is never asked about on its own, so never shut as deleted.
    """
    found = _BOARD_PAGE.fullmatch(url)
    return (
        found is not None
        and found["owner"].casefold() == owner.casefold()
        and found["number"] == str(number)
    )


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
