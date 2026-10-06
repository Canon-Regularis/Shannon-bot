"""Reading a GitHub project board over REST.

REST rather than GraphQL, to keep one transport. Every path is prefixed by the kind of account
that owns the board, and the two prefixes are not interchangeable. A login names one account of
one kind — GitHub keeps users and organisations in a single namespace — so the wrong prefix is
not a longer route to the same board, it is a 404, which is what every organisation's board
answered here until this asked rather than assumed.

That is a different fact from the one the owner setting guards, and they are easy to run
together. A project number is a sequence GitHub keeps per OWNER, so the same number under a
DIFFERENT owner is a real board holding somebody else's cards. The prefix cannot take you there;
a wrongly guessed owner can.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote

from shannon.domain.board import columns_for, normalise, status_from_column
from shannon.domain.enums import ObjectType, Priority, Status, spoken
from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.domain.models import Actor, Label
from shannon.domain.priority import parse_priority
from shannon.github import mapping
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from shannon.github.paging import PagedRead

logger = logging.getLogger(__name__)


# The board column lives in the single-select field GitHub's own templates call Status. A board
# that renamed it cannot be read.
STATUS_FIELD = "Status"
TITLE_FIELD = "Title"
# Optional, unlike the two above. GitHub's default template does not ship one, and a board
# without it mirrors perfectly well - it simply cannot be told a priority.
PRIORITY_FIELD = "Priority"

# The rest of what a card's block shows, since issue #182. All optional, all matched by NAME, and
# the names are the board owner's to change - so a board calling one of these something else has no
# value for that row, which is a row left out rather than a read that fails.
#
# Spelled exactly as the board spells them. `Story Point` is SINGULAR on the board this was built
# against and holds zero-padded options rather than numbers, so the plural anybody would write by
# reflex matches nothing - silently, on every card, for ever. That is worth a line of comment.
ASSIGNEES_FIELD = "Assignees"
STORY_POINT_FIELD = "Story Point"
ITERATION_FIELD = "Iteration"
AREA_FIELD = "Area"
LABELS_FIELD = "Labels"

# What `fields=` on the item read is built from: every field anything here parses.
#
# It was Title and Status alone until #182, and the comment on `BoardFields.wanted` explained why -
# "nothing reads a card's priority back, so naming Priority here would send a field per card per
# poll that nothing parses". Something reads them all back now. The cost is bytes rather than
# requests: the same one call per page per poll, carrying more of each card.
READ_FIELDS = (
    TITLE_FIELD,
    STATUS_FIELD,
    PRIORITY_FIELD,
    ASSIGNEES_FIELD,
    STORY_POINT_FIELD,
    ITERATION_FIELD,
    AREA_FIELD,
    LABELS_FIELD,
)

# GitHub's maximum.
PAGE_SIZE = 100

# The ways a board refuses to open, which three callers catch together and none can tell
# apart. A board GitHub does not have answers 404; an authorisation that may not see it
# answers 403, and one revoked or expired answers 401. A board nobody's authorisation stands
# behind is not asked at all: `_read_as` refuses it before any request, with the same
# GitHubAuthError. An operator cannot act on the difference and the log carries which it
# was, so the three sites that read a board fold them into one answer each.
UNREADABLE = (GitHubNotFoundError, GitHubAuthError)

# What GitHub calls the thing a card wraps, mapped to what this bot calls it. Issues and pull
# requests are already mirrored from their own webhooks, so a card is looked up, not created.
CONTENT_TYPES: dict[str, ObjectType] = {
    "DraftIssue": ObjectType.TICKET,
    "Issue": ObjectType.ISSUE,
    "PullRequest": ObjectType.PR,
}


@dataclass(frozen=True, slots=True)
class ProjectListing:
    """One board, as somebody choosing between them needs it.

    No owner on it. A listing is always read under one owner and handing that owner back would
    invite a caller to trust the copy rather than the question it asked.
    """

    number: int
    title: str


class CardMove(StrEnum):
    """What became of an attempt to move a card.

    Not a bool, because only one of the four ways of declining is worth telling a person
    about. The other three are properties of the deployment or of a board that cannot be
    read at all - invisible, and identical for every command until somebody changes
    something. NO_COLUMN is visible, because they will open the board and find the card
    where it was.
    """

    MOVED = "moved"
    NO_WRITER = "no writer"
    UNREADABLE = "unreadable"
    NO_FIELD = "no field"
    NO_COLUMN = "no column"


@dataclass(frozen=True, slots=True)
class CardMoved:
    """What became of a card write, and the column it landed in.

    The NAME rather than the state that was asked for, and that is the whole reason
    this is a pair rather than the outcome alone. A board may spell a column anything
    this bot reads as the state - `In progress` for IN_REVIEW, `Todo` for
    NOT_REVIEWED - and the poller compares the column it last saw by text. Recording
    what was ASKED FOR rather than what was WRITTEN would disagree with the board for
    ever on any such board, once per poll, instead of once per command.
    """

    outcome: CardMove
    # Empty unless something was written, and read by nothing in that case.
    column: str = ""


@dataclass(frozen=True, slots=True)
class BoardOption:
    """One choice in the board's Status field.

    The id is a STRING while a field id is an int, and they are not interchangeable: the field
    id is a path segment and this is a value in the body. Two separate types rather than two
    ints, so putting one where the other belongs is a type error rather than a 422.
    """

    option_id: str
    name: str


@dataclass(frozen=True, slots=True)
class BoardSelect:
    """One single-select field: the id a write addresses, and what it may be set to.

    The two travel together because neither is any use alone - an id with no options is a
    field nothing can be written to, and options with no id have nowhere to go.
    """

    field_id: int
    # In the order GitHub gave them, which is left to right on the board.
    options: tuple[BoardOption, ...]


@dataclass(frozen=True, slots=True)
class BoardOrder:
    """A board's Status columns in board order, and the ones two statuses map onto.

    Both come from the same picker `move_card` uses, so a rule built on this cannot disagree
    with the write it is about to allow or refuse. Empty where the board has no column for that
    status, which reads as nothing to reason about rather than as a column called "".
    """

    # Left to right on the board, which is the only ordering GitHub gives: a single-select
    # option carries no position, rank or index, and the array order is the whole of it.
    columns: tuple[str, ...]
    leaving: str
    arriving: str


@dataclass(frozen=True, slots=True)
class BoardFields:
    """What one board's `/fields` answer is worth keeping, looked up once per board.

    Status is required and Priority is not, and that difference is in the types rather than
    in a convention: a board with no Status field cannot be mirrored at all, so this whole
    object is None for one, while a board with no Priority field is ordinary and simply
    cannot be told a priority.

    Neither is read back out of `wanted`. That tuple is built positionally and is
    `(status_id,)` on a board with no Title field and `(title_id, status_id)` with both, so
    indexing it is correct only because the no-Status arm returns before it is ever cached -
    a positional invariant held by an unrelated `if` twenty lines away. That is the kind of
    thing that survives review and fails on a real board.
    """

    # What `list_board_items` sends as `fields=`: the id of every field in `READ_FIELDS` the
    # board actually has. Nothing indexes it, which is the point - `status` and `priority` below
    # are kept as their own members precisely so no caller has to know this tuple's order.
    wanted: tuple[int, ...]
    status: BoardSelect
    priority: BoardSelect | None = None


@dataclass(frozen=True, slots=True)
class BoardItem:
    """One card on a board, as this service needs it.

    Everything below `content_id` arrived with issue #182, and all of it is optional in the same
    way `column` already was: a board without the field, or a card with nothing set in it, answers
    None or an empty tuple, and the block leaves that row out.

    The selects are carried as the board's own option TEXT rather than as one of this project's
    enums, which is what `column` already does and for the same reason: the mapping from a column
    to a status is a domain decision and lives with the policies, not in a parser.
    """

    item_id: int
    title: str
    column: str | None
    html_url: str
    kind: ObjectType = ObjectType.TICKET
    updated_at: datetime | None = None
    # GitHub's id for the issue or pull request the card wraps, which is the id that item was
    # already stored under when its own webhook arrived. None for a draft, which wraps nothing.
    content_id: int | None = None
    # Off the ITEM rather than out of its fields: these describe the card's place on the board,
    # not a column somebody configured, so they are there whatever fields the board has.
    creator: Actor | None = None
    created_at: datetime | None = None
    # A DRAFT card's own text, which is the one thing here that is not a project field and not on
    # the item either: it sits under `content`, beside the draft's title. An issue or a pull request
    # has its body mirrored from its own webhook long before a board is read, so this is only ever
    # read for a draft - and for a draft it is the only place the text exists at all.
    body: str = ""
    # Out of the fields list, each one only where the board has that field.
    assignees: tuple[Actor, ...] = ()
    labels: tuple[Label, ...] = ()
    priority_name: str | None = None
    story_point: str | None = None
    iteration: str | None = None
    area: str | None = None
    # Whether the card is in the board's archive. Issue #198: an archived card used to be dropped
    # where it was read, which made it look exactly like a deleted one - absent - so neither could
    # be told to its thread. Kept and marked instead, and everything that mirrors a card leaves an
    # archived one alone. Last, so every construction from before it still means what it did.
    archived: bool = False

    @property
    def is_draft(self) -> bool:
        return self.kind is ObjectType.TICKET


class WritesJson(Protocol):
    """Sending JSON at a path, which is all a board write needs.

    Its own Protocol beside the reader rather than a method on `GitHubClient`. That one carries
    `get_json` only because the wiring hands the same object to the board reader as its
    transport, every read carrying a person's authorisation; putting a PATCH there would hand
    every service in this project the ability to write to any path with an installation token,
    which is strictly wider than anything else on it and is exactly what the module docstring
    says a handle must not be.
    """

    async def patch_json(
        self, path: str, *, owner: str, token: str = "", json: JsonObject
    ) -> None: ...


class ReadsJson(Protocol):
    """Fetching JSON, one body or a page at a time."""

    async def get_json(
        self, path: str, *, owner: str = "", token: str = "", **params: str | int
    ) -> object: ...

    def get_pages(
        self, path: str, *, owner: str = "", token: str = "", **params: str | int
    ) -> AsyncIterator[object]: ...

    async def get_pages_since(
        self,
        path: str,
        *,
        etag: str | None = None,
        owner: str = "",
        token: str = "",
        **params: str | int,
    ) -> PagedRead: ...


class WhoTheBoardIsReadAs(Protocol):
    """The authorisation a linked board's own reads are made under, or nothing where it has none.

    One member, declared here because this is where it is consumed. Issue #170.

    **Why not `SuppliesTokens`.** That one answers `token_for(owner)`, and an owner cannot identify
    a person: `mirroring` refuses two repositories on the SAME board, but two servers may
    link two DIFFERENT boards both owned by the same account. Keyed on the owner, one server's
    board would be read under the other server's member's grant - a credential crossing a tenancy
    boundary, which is the exact thing the authorisation replaced.

    So a board's reads are keyed on the board. Its own reads only: the items, the Status field, the
    column order. A WRITE is the opposite and takes an explicit credential from the caller, because
    a card moved as whoever happened to link the board is what this change exists to stop.
    """

    async def reading(self, owner: str, project_number: int) -> str: ...


class HttpProjectBoards:
    """`ReadsBoards` on top of GitHub's REST API for project boards."""

    def __init__(
        self,
        client: ReadsJson,
        credentials: WhoTheBoardIsReadAs,
        *,
        writer: WritesJson | None = None,
    ) -> None:
        self._client = client
        self._credentials = credentials
        # None where SHANNON_BOARD_MAY_MOVE_CARDS is off: `may_write` reports it and `move_card`
        # answers NO_WRITER. The writer carries no credential of its own - every write passes
        # the mover's as `as_` - so leaving it None makes "writes off means no board writes" a
        # fact of the wiring rather than a check somebody could forget.
        self._writer = writer
        self._fields: dict[tuple[str, int], BoardFields] = {}
        self._kinds: dict[str, str] = {}
        # The last read of each board, as {(owner, number): (validator, the cards it describes)}.
        #
        # This is what makes a two-second poll cost less than the one-minute poll it replaced. The
        # items endpoint answers a conditional GET with 304 and no body, and a 304 spends NO
        # rate-limit budget at all, so a board nobody touched is read for nothing. The cards are
        # kept beside the validator because a 304 proves the body was identical, which makes the
        # parse that produced them reproducible rather than merely likely.
        #
        # In memory and not a table. A restart costs one full read, which is the cheapest possible
        # price for not having a migration, and nothing here is worth surviving a deploy.
        self._listed: dict[tuple[str, int], tuple[str, tuple[BoardItem, ...]]] = {}
        # Boards and owners already complained about, so each complaint is loud once and then
        # drops to DEBUG. Both paths re-read on every poll by design, and issue #189 turned that
        # from a line a minute into a line every couple of seconds.
        self._fieldless: set[tuple[str, int]] = set()
        self._unnamed: set[str] = set()
        # Boards already complained about for one state, so the complaint is said once
        # rather than once per command. Keyed on the state too: a board with no column for
        # one status very likely has one for another, and keying on the board alone would
        # swallow a different complaint. Never expires, and the reason it needs no expiry is
        # `order_for` above: a command re-reads the board's fields, so the moment somebody adds
        # the column the picker finds it and this is never reached again. Before that read was
        # fresh, adding the column changed nothing until a restart while this swallowed the
        # warning - so the log fell silent, which reads as fixed, and the card still never moved.
        self._warned: set[tuple[str, int, str]] = set()

    async def list_boards(self, owner: str, *, token: str = "") -> Sequence[ProjectListing]:
        """Every board this owner has, for somebody choosing one.

        Over the same prefix the reads use, so the organisation-or-person decision is made once
        and a picker cannot offer a board the poller then cannot open.

        Under the credential of whoever is choosing, on every page. Issue #201: the listing used to
        go out with none, which the client answers with the App installation's token - and the
        App holds no Projects permission, so a private board was never offered to the person who
        could open it.
        """
        kind = await self._owner_kind(owner, token)
        listings: list[ProjectListing] = []
        async for body in self._client.get_pages(
            f"/{kind}/{quote(owner, safe='')}/projectsV2",
            owner=owner,
            token=token,
            per_page=PAGE_SIZE,
        ):
            rows = body if is_json_list(body) else []
            listings.extend(listing for row in rows if (listing := parse_listing(row)) is not None)
        return listings

    async def get_board(
        self, owner: str, project_number: int, *, token: str = ""
    ) -> ProjectListing | None:
        """One board, or None where this token cannot open it.

        Exists so that "the token cannot see this board" becomes a refusal the person who typed
        it reads, rather than a warning once a minute in a log nobody is watching. A picker's
        suggestions are only suggestions - discord.py says so - so the number that arrives here
        may never have been offered.

        The account lookup is inside the same fold as the board itself. It is the FIRST request
        made with the person's credential, so a grant they revoked on GitHub answers 401 there and
        a mistyped owner answers 404 there - and outside the fold both reached the person as
        GitHub's raw sentence about a `/users/` path, which names neither the board nor the
        credential. Issue #201.
        """
        try:
            board = await self._board_path(owner, project_number, token)
            body = await self._client.get_json(board, owner=owner, token=token)
        except UNREADABLE as unopenable:
            # Logged before it is folded, because folding is what costs the evidence. The reply
            # lists what to check and cannot say WHICH, so without this line the one hard fact -
            # 401 and 403 mean a credential and 404 means a board - is thrown away at the moment
            # somebody most needs it.
            logger.warning("could not open board %s for %r: %s", project_number, owner, unopenable)
            # Folded into the answer this already gives for an unusable board, because an
            # operator cannot act on the difference and the caller has one sentence to say.
            # A board GitHub does not have, a token that may not see it, and a token that is
            # not there at all are 404, 403 and 401 - and GitHub's own words for those name
            # neither the board nor the credential, which is every question worth asking.
            return None
        return parse_listing(body)

    async def move_card(
        self,
        *,
        owner: str,
        project_number: int,
        card_id: int,
        state: Status | Priority,
        as_: str,
        column: str = "",
    ) -> CardMoved:
        """Set the card's Status or Priority to match, saying what became of it.

        One method over the union rather than two, because the CALLEE picks the field: a
        caller physically cannot hand a priority to the Status field. `spoken` is already
        typed over the same union and `_refuse_a_name_this_bot_owns` already branches on it.

        An answer rather than a bool, because the ways of declining are not alike and one of
        them is worth telling a person about. Board writes turned off and an unreadable Status
        field are both invisible on the board and identical for every command; a board with no
        column for this state is permanent until somebody edits it AND visible, because the
        person will open the board and find the card where it was.

        UNSET never arrives. The picker offers three priorities and `priority_change` raises
        on a fourth long before this, so a guard here would be an arm no caller can take -
        which matters more than it sounds, since `spoken(Priority.UNSET)` is "None" and
        would match a board column literally called None.

        `column` is the board's own column name where somebody picked one, and it wins over the
        state's own mapping. It is what makes a board with two columns for one status usable:
        GitHub's default template ships `In progress` and `In review` and both read as IN_REVIEW,
        so without a name to go on the exact-name pass takes `In review` every time and nothing
        could ever put a card in `In progress`. Empty for `/priority`, which has one field and
        no such ambiguity, and for anything that has only a status to go on.
        """
        if self._writer is None:
            return CardMoved(CardMove.NO_WRITER)

        board = await self._board_path(owner, project_number, as_)
        fields = await self._board_fields(board, owner, project_number, as_)
        if fields is None:
            return CardMoved(CardMove.UNREADABLE)

        select = fields.status if isinstance(state, Status) else fields.priority
        if select is None:
            return CardMoved(CardMove.NO_FIELD)

        option = _option_named_or_for(select.options, state, column)
        if option is None:
            # Dropped before the complaint, because not finding a column is exactly when this
            # board's cached options are worth doubting. They never expired, so an operator who
            # did what the complaint tells them - add a column, rename one - saw nothing change
            # until the process restarted, and `_warned` then swallowed the repeat, so the log
            # fell silent, which reads as fixed while the card still never moved.
            #
            # `/status` no longer needs this: `order_for` refreshes on its way past. `/priority`
            # does, because it writes with no rule to check first and so never refreshes.
            self._fields.pop((owner, project_number), None)
            self._complain(owner, project_number, state, card_id)
            return CardMoved(CardMove.NO_COLUMN)

        # As the person who asked, which is the whole of issue #170 in one argument. Before it,
        # every card move went out under one shared token and GitHub recorded that account as
        # having moved it, whoever had actually dragged anything.
        await self._writer.patch_json(
            f"{board}/items/{card_id}",
            owner=owner,
            token=as_,
            json={"fields": [{"id": select.field_id, "value": option.option_id}]},
        )
        return CardMoved(CardMove.MOVED, column=option.name)

    def _complain(
        self, owner: str, project_number: int, state: Status | Priority, card_id: int
    ) -> None:
        """Say a board has nowhere to put this, once per board per state.

        Nothing retries a card write, so this cannot repeat on its own - the ceiling is the
        rate people run the command, and a team with no merge-gate column would otherwise
        write a byte-identical line dozens of times a day.

        The owner is named as well as the number, for the reason the unreadable-board
        warning already gives at length: a board number is a sequence GitHub keeps per
        account, so it means nothing alone now that several repositories can each carry one.

        And the state is spelled the way the picker spells it. `status.value` prints
        READY_FOR_MERGE at somebody who chose `Ready for merge`.
        """
        said = spoken(state)
        key = (owner, project_number, said)
        if key in self._warned:
            return
        self._warned.add(key)

        wanted = ", ".join(columns_for(state)) if isinstance(state, Status) else said.lower()
        logger.warning(
            "board %s/%s has no column this bot reads as %s, so card %s was left where it "
            "was. A column called any of: %s would be, in any capitalisation",
            owner,
            project_number,
            said,
            card_id,
            wanted,
        )

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        """Every card on the board, the archived ones marked rather than dropped.

        Archiving is how a card is taken off the board without deleting it, so mirroring one
        would put back a thread for work already put away - and nothing here mirrors one. They
        used to be dropped right here, which made an archived card and a deleted one the same
        thing to the poller: absent. Issue #198 needs them apart, because an archived card's thread
        comes back with the card and a deleted card's never does.

        Whether GitHub lists archived cards at all is not something its documentation says. This
        is right either way: a listed one arrives marked, and one left out is a card missing from
        the read, which the poller asks about on its own.
        """
        token = await self._read_as(owner, project_number)
        board = await self._board_path(owner, project_number, token)
        fields = await self._board_fields(board, owner, project_number, token)
        params: dict[str, str | int] = {"per_page": PAGE_SIZE}
        if fields is not None:
            params["fields"] = ",".join(str(field) for field in fields.wanted)

        key = (owner, project_number)
        read = await self._client.get_pages_since(
            f"{board}/items", etag=self._validator(key), owner=owner, token=token, **params
        )
        if read.pages is None:
            # GitHub said nothing changed, so the cards parsed last time ARE this read's answer.
            # Sound by construction rather than by hope: a 304 means the body was byte-identical,
            # and `parse_item` is a pure function of the body, so re-parsing could only produce
            # what is already here.
            #
            # Indexed rather than fetched defensively. This line is reachable only having SENT a
            # validator, and `_validator` only ever answers with one taken from this same entry,
            # so the entry exists whenever this runs.
            return self._listed[key][1]

        rows = [row for body in read.pages for row in (body if is_json_list(body) else [])]
        items = tuple(item for row in rows if (item := parse_item(row, project_number)) is not None)

        # Two conditions, and the second is not belt-and-braces. A validator may only be kept for
        # a board that provably arrived WHOLE, because GitHub's ETag hashes one response body:
        #
        #  - more than one page, and it speaks for the first of them only;
        #  - a FULL page, and it cannot prove there is nothing behind it. A board of exactly
        #    PAGE_SIZE cards that gains one leaves page one byte-identical, so a 304 against that
        #    validator would hide the new card for ever - and a 304 carries no Link header to ask.
        #
        # A page that is not full cannot be hiding anything, which is the one case worth trusting.
        if read.etag is not None and len(rows) < PAGE_SIZE:
            self._listed[key] = (read.etag, items)
        else:
            # Dropped rather than left to go stale. Correctness does not need this - GitHub
            # validates against the live body, so a stale validator answers 200 - but an entry
            # that cannot be trusted is not worth the reading.
            self._listed.pop(key, None)
        return items

    async def read_card(self, owner: str, project_number: int, card_id: int) -> BoardItem | None:
        """One card read on its own, or None where GitHub has no such card on this board.

        Issue #198. A card the board's listing has stopped showing may have been deleted, archived
        out of a listing that leaves archived cards out, or simply missed by a read that could not
        prove it arrived whole - so the poller asks about that one card before it believes any of
        them.

        None means exactly one thing: GitHub answered THIS card's own path with 404. The poller
        reads that as deleted, which lets a thread go for good, so nothing else may become None.
        The credential and the board's path are found outside the `try` for that reason - a 404
        looking up the account is a board that will not open, not a card that has gone - and a body
        that is not a card raises rather than answering None, because "GitHub said something odd"
        is not "GitHub has no such card".

        Under the board's own reader, as every other read of the board is: its linker's
        authorisation, and no request at all where nobody's stands behind it.
        """
        token = await self._read_as(owner, project_number)
        board = await self._board_path(owner, project_number, token)
        try:
            body = await self._client.get_json(f"{board}/items/{card_id}", owner=owner, token=token)
        except GitHubNotFoundError:
            return None
        card = parse_item(body, project_number)
        if card is None:
            raise GitHubUnavailableError(
                f"GitHub answered card {card_id} on board {project_number} belonging to {owner} "
                "with something that is not a card"
            )
        return card

    async def _read_as(self, owner: str, project_number: int) -> str:
        """The credential a board's own reads go out under, refusing before any request without one.

        Issue #201. `reading` answers an empty string for a board nobody's authorisation stands
        behind - nobody linked it, the linker withdrew, two servers claim it - and an empty
        credential is not "anonymous" on its way through the client: it is filled in with the App
        installation's token for the owner. That only ever failed because the App holds no Projects
        permission today, which is a thing an installation can be granted. So no authorisation means
        no request, refused here as the authorisation failure it is - the same exception a 401 or a
        403 would raise, which every caller of these three reads already treats as a board that will
        not open.
        """
        token = await self._credentials.reading(owner, project_number)
        if not token:
            raise GitHubAuthError(
                f"nobody's authorisation stands behind board {project_number} belonging to {owner}"
            )
        return token

    async def takes(self, *, owner: str, project_number: int, state: Status | Priority) -> bool:
        """Whether this board has the field a state is written to. See `MovesCards.takes`.

        Read as the BOARD is read - its linker's authorisation, and no request at all where
        nobody's stands behind it - because the member asking has none, which is why this is
        being asked. Out of the same cached fields `move_card` resolves against, so the
        question and the write cannot disagree about whether the field is there.
        """
        token = await self._read_as(owner, project_number)
        board = await self._board_path(owner, project_number, token)
        fields = await self._board_fields(board, owner, project_number, token)
        return fields is not None and (
            fields.priority is not None if isinstance(state, Priority) else True
        )

    @property
    def may_write(self) -> bool:
        """Whether a card can be written at all, which is whether a writer was handed over.

        `SHANNON_BOARD_MAY_MOVE_CARDS` is applied here, to the writer, rather than by withholding
        this whole object - see the container, and issue #179, where withholding it silently took
        the column-order rule with it. So this is the only place that can answer the question.
        """
        return self._writer is not None

    def _validator(self, key: tuple[str, int]) -> str | None:
        """The ETag to ask with, when one has been kept for this board."""
        remembered = self._listed.get(key)
        return remembered[0] if remembered else None

    def can_recheck_cheaply(self, owner: str, project_number: int) -> bool:
        """Whether reading this board again would cost a conditional request or a megabyte.

        True once a read has kept a validator, which happens only for a board that arrived whole
        in one unfull page. The poller asks because its cadence has to follow what a re-read
        costs, and only this object knows: a board that can be checked for nothing is worth
        checking every couple of seconds, and a board that cannot be checked without downloading
        the whole of it is emphatically not.

        False before the first read, which is the right answer rather than a missing one - nothing
        has been proven cheap yet.
        """
        return (owner, project_number) in self._listed

    async def _board_path(self, owner: str, project_number: int, token: str = "") -> str:
        """Where this board lives, which the kind of account owning it decides.

        Worked out once per read and handed down, rather than rebuilt by each caller that wants
        it: a second call would hit the cache below and cover its branch incidentally, leaving
        the test that the kind is asked for only once proving nothing.
        """
        kind = await self._owner_kind(owner, token)
        return f"/{kind}/{quote(owner, safe='')}/projectsV2/{project_number}"

    async def _owner_kind(self, owner: str, token: str = "") -> str:
        """`orgs` or `users`, asked of GitHub once per account and then kept.

        Asked rather than configured because an operator can get it wrong and GitHub cannot,
        and because an account converted to an organisation would leave a setting stale and
        every poll afterwards asking under a prefix that has stopped answering.

        An answer that cannot be read is taken as a person — which is what every board did
        before this existed — and is NOT remembered, so a blip does not decide the prefix for
        the life of the process.
        """
        if owner in self._kinds:
            return self._kinds[owner]

        body = await self._client.get_json(
            f"/users/{quote(owner, safe='')}", owner=owner, token=token
        )
        account: JsonObject = body if is_json_object(body) else {}
        kind = account.get("type")
        if not isinstance(kind, str):
            # Loud once per owner, then quiet, for the reason the fields warning above is: the
            # guess is deliberately not remembered, so this path repeats on every poll, and issue
            # #189 made that thirty times an hour into eighteen hundred. The request it repeats is
            # left alone - the comment above refuses to let a blip decide the prefix for the life
            # of the process, and that judgement is not this change's to overturn.
            level = logging.WARNING if owner not in self._unnamed else logging.DEBUG
            self._unnamed.add(owner)
            logger.log(
                level,
                "GitHub did not say what kind of account %r is, so its board is read as a "
                "person's; an organisation's board will answer 404 until it does",
                owner,
            )
            return "users"

        self._unnamed.discard(owner)

        decided = "orgs" if kind == "Organization" else "users"
        self._kinds[owner] = decided
        return decided

    async def status_columns(self, owner: str, project_number: int) -> tuple[str, ...]:
        """This board's Status columns, in board order, for a picker to offer.

        Read fresh, the same way `order_for` reads fresh and for a sharper reason: this is the
        list somebody chooses FROM, so a stale one offers a column the board no longer has and
        hides one it does. The caller in front of this keeps the answer for a couple of minutes,
        which is what stops a keystroke being a GitHub call.

        Empty rather than None for a board that cannot be read. A picker has three seconds and
        nowhere to put a refusal, so having nothing to offer and having nothing to say are the
        same outcome to it.
        """
        token = await self._read_as(owner, project_number)
        board = await self._board_path(owner, project_number, token)
        self._fields.pop((owner, project_number), None)
        fields = await self._board_fields(board, owner, project_number, token)
        if fields is None:
            return ()
        return tuple(one.name for one in fields.status.options)

    async def order_for(
        self, *, owner: str, project_number: int, frm: Status, to: Status, column: str = ""
    ) -> BoardOrder | None:
        """This board's Status columns in order, with the columns two statuses map onto.

        Read fresh, by dropping the cached fields first. That cache exists so a poll does not
        re-read a board it has already read, and it never expired - so a column added, renamed
        or REORDERED had no effect until the process restarted. Tolerable for a write, which
        fails visibly and tells whoever ran it to rename a column. Not tolerable for a rule
        derived from the order, which would go on quietly judging moves against a board
        somebody rearranged an hour ago and never say a word about it.

        So one extra read per command - on a path a person drives, not a poll - and the write
        that follows reuses what this just cached, which is what keeps the rule and the write
        agreeing about where a status goes.

        None where the board cannot be read at all, which the caller reads as no rule to apply.
        """
        token = await self._read_as(owner, project_number)
        board = await self._board_path(owner, project_number, token)
        self._fields.pop((owner, project_number), None)
        fields = await self._board_fields(board, owner, project_number, token)
        if fields is None:
            return None

        options = fields.status.options
        leaving = _option_for(options, frm)
        # Resolved exactly as the write will resolve it, which is the point of the shared
        # helper: a rule measuring to one column while the write goes to another would refuse
        # moves it then made.
        arriving = _option_named_or_for(options, to, column)
        return BoardOrder(
            columns=tuple(one.name for one in options),
            leaving=leaving.name if leaving is not None else "",
            arriving=arriving.name if arriving is not None else "",
        )

    async def _board_fields(
        self, board: str, owner: str, project_number: int, token: str = ""
    ) -> BoardFields | None:
        """The Title and Status fields, and the Status field's choices, once per board.

        Items come back carrying only their Title unless the request names the field ids it
        wants, so this is read to list a board at all. The choices ride along because they
        are in the same answer: a write needs the id of the option it is moving a card to,
        and asking a second time for something already on the wire would be a second call
        per board per process for nothing.

        None rather than an empty tuple for a board with no Status field, and NOT cached.
        Without that id every card reads as having no column, which is a shape nothing above
        may believe, and the next read may get an answer that has it - a renamed field is one
        edit to undo and the poll after it should see the board again. One entry holds all
        three facts so that rule cannot be honoured for the ids and missed for the options:
        two caches can disagree about whether a board has a Status field, and one cannot.
        """
        key = (owner, project_number)
        if key in self._fields:
            return self._fields[key]

        body = await self._client.get_json(f"{board}/fields", owner=owner, token=token)
        rows = body if is_json_list(body) else []
        named = {
            row.get("name"): row
            for row in rows
            if is_json_object(row)
            and row.get("name") in READ_FIELDS
            and isinstance(row.get("id"), int)
        }
        status = named.get(STATUS_FIELD)
        if status is None:
            # Loud once per board, then quiet. This answer is deliberately not cached, so the read
            # repeats on every poll - sixty lines an hour before issue #189 and eighteen hundred
            # after it. A warning that repeats that often is one whoever reads the log learns to
            # scroll past, which costs more than the line is worth, and what it reports cannot
            # change between two polls. Dropped to DEBUG rather than silenced, so a log turned up
            # on purpose still shows the board being re-read.
            #
            # The level is a one-line conditional because the coverage floor counts the arms of an
            # `if`, and there is nothing to say in either that is not said here.
            level = logging.WARNING if key not in self._fieldless else logging.DEBUG
            self._fieldless.add(key)
            logger.log(
                level,
                "project %s answered with no %r field, so no card can carry a status",
                project_number,
                STATUS_FIELD,
            )
            return None

        # Found after all, so the next board to lose it is a new complaint rather than a repeat.
        self._fieldless.discard(key)

        status_id = status["id"]
        assert isinstance(status_id, int)
        found = BoardFields(
            wanted=tuple(
                field_id
                for name in READ_FIELDS
                if (row := named.get(name)) is not None
                and isinstance(field_id := row.get("id"), int)
            ),
            status=BoardSelect(field_id=status_id, options=_options_of(status)),
            priority=_select_of(named.get(PRIORITY_FIELD)),
        )
        self._fields[key] = found
        return found


def _option_named(options: Sequence[BoardOption], wanted: str) -> BoardOption | None:
    """The column called exactly this, however either side spells it.

    Both sides go through `normalise`, so a board saying `HIGH` and this bot saying `High`
    are the same word.
    """
    asked = normalise(wanted)
    return next((one for one in options if normalise(one.name) == asked), None)


def _reads_as(name: str, state: Status | Priority) -> bool:
    """Whether a column name is a SYNONYM for this state.

    Both tables are many-to-one and both are already the single source for their half:
    `status_from_column` for a column, `parse_priority` for a label. The second answers
    UNSET where the first answers None, so it is folded to None here the way
    `labels.reserved_as` already folds it.
    """
    if isinstance(state, Status):
        return status_from_column(name) is state
    found = parse_priority([name])
    return found is not Priority.UNSET and found is state


def _option_named_or_for(
    options: Sequence[BoardOption], state: Status | Priority, column: str
) -> BoardOption | None:
    """The column somebody named, or the one this state maps to where they named none.

    One resolver for both the write and the rule that decides whether the write is allowed. They
    used to reach the same answer by two routes and only because both routes were `_option_for`;
    now that a person can name a column the picker showed them, a rule measuring to one column
    while the write went to another would refuse moves it then made, or make moves it had just
    approved somewhere else.

    A name that is not on this board falls back rather than failing. It arrives from a picker
    that had three seconds and may have shown nothing at all, so what reaches here may be typed
    prose or a column from the board this repository mirrored last week.
    """
    if column:
        named = _option_named(options, column)
        if named is not None:
            return named
    return _option_for(options, state)


def _option_for(options: Sequence[BoardOption], state: Status | Priority) -> BoardOption | None:
    """The board's own column standing for this state, or None where it has none.

    Two passes, and the first one is not an optimisation. The words a board may use are read
    through tables that are many-to-one on purpose - `In progress` and `In review` both mean
    IN_REVIEW, `urgent` and `critical` both mean HIGH - because a board using either is
    saying the same thing. That is right for READING a column back.

    Writing one, it is not enough. A board carrying BOTH columns is the ordinary case rather
    than a corner - GitHub's own default template ships Backlog, Ready, In progress, In
    review, Done - and picking whichever came first would move a card to `In progress` when
    somebody asked for `In review`. So a column called exactly what this bot calls the state
    wins over one that merely maps to it.

    The fallback keeps board order, which is left to right. It only decides between synonyms
    now, where neither is the state's own name and there is nothing better to go on.
    """
    exact = _option_named(options, spoken(state))
    if exact is not None:
        return exact
    return next((one for one in options if _reads_as(one.name, state)), None)


def _select_of(field: JsonObject | None) -> BoardSelect | None:
    """One optional single-select, or None where the board has nothing usable.

    A field with no readable options folds to None rather than to an empty select, so
    "nothing to write here" is one shape instead of two. Status does not take this route:
    its id is read for every poll whether or not its options parse, because it is what the
    request asks for by id.
    """
    if field is None:
        return None
    field_id = field.get("id")
    options = _options_of(field)
    if not isinstance(field_id, int) or not options:
        return None
    return BoardSelect(field_id=field_id, options=options)


def _options_of(field: JsonObject) -> tuple[BoardOption, ...]:
    """The choices a single-select field offers, in the order GitHub listed them.

    Order is kept because it is the board's own left-to-right order, and that is the tiebreak
    when two columns mean the same thing to this bot.

    Every row is checked before it is read, like every other shape in this module: these came
    from published documentation rather than from a board, so a row that turns out to differ
    costs one unpickable column rather than a poll that dies.
    """
    listed = field.get("options")
    rows = listed if is_json_list(listed) else []
    return tuple(
        BoardOption(option_id=option_id, name=name)
        for row in rows
        if is_json_object(row)
        and isinstance(option_id := row.get("id"), str)
        and (name := _text(row.get("name"))) is not None
    )


def parse_item(payload: object, project_number: int) -> BoardItem | None:
    """One card, or None for one this bot cannot make sense of.

    The poller uses a card's content id to find the thread its issue or pull request already
    has, rather than opening a second one.

    An archived card is read like any other and marked, rather than dropped here. Issue #198:
    dropped, it was indistinguishable from a deleted card, and the two ask different things of a
    thread.
    """
    if not is_json_object(payload):
        return None

    # Checked for being a string before it is looked up: a dict.get on an unhashable key raises
    # TypeError rather than answering None, and one malformed card would end the whole poll.
    content_type = payload.get("content_type")
    kind = CONTENT_TYPES.get(content_type) if isinstance(content_type, str) else None
    if kind is None:
        return None

    item_id = payload.get("id")
    if not isinstance(item_id, int):
        return None

    wrapped = payload.get("content")
    content: JsonObject = wrapped if is_json_object(wrapped) else {}

    listed = payload.get("fields")
    fields: list[object] = listed if is_json_list(listed) else []

    content_id = content.get("id")
    title = _text(_field_value(fields, TITLE_FIELD)) or _text(content.get("title"))
    if not title:
        return None

    return BoardItem(
        item_id=item_id,
        kind=kind,
        title=title,
        column=_text(_option_name(_field_value(fields, STATUS_FIELD))),
        # A draft has no page of its own, so the board is the nearest true link.
        html_url=_text(content.get("html_url")) or _board_url(payload, project_number),
        updated_at=mapping.parse_timestamp(payload.get("updated_at")),
        content_id=content_id if isinstance(content_id, int) else None,
        # Issue #182. Every one of these goes through a `mapping` parser that already tolerates
        # the field being absent, the value being null and the shape being wrong - so a board
        # missing a field needs no guard here, it simply answers None or an empty tuple.
        creator=mapping.actor(payload.get("creator")),
        created_at=mapping.parse_timestamp(payload.get("created_at")),
        body=_text(content.get("body")) or "",
        assignees=mapping.actors(_field_value(fields, ASSIGNEES_FIELD)),
        labels=mapping.labels(_field_value(fields, LABELS_FIELD)),
        priority_name=_text(_option_name(_field_value(fields, PRIORITY_FIELD))),
        story_point=_text(_option_name(_field_value(fields, STORY_POINT_FIELD))),
        iteration=_iteration_title(_field_value(fields, ITERATION_FIELD)),
        area=_text(_option_name(_field_value(fields, AREA_FIELD))),
        archived=payload.get("archived_at") is not None,
    )


def parse_listing(payload: object) -> ProjectListing | None:
    """One board out of a listing, or None for a shape this bot cannot use.

    A board with no title is not refused for tidiness: the title is the whole of what a picker
    shows, and an entry reading `#7` with nothing beside it is a choice nobody can make.
    """
    if not is_json_object(payload):
        return None

    number = payload.get("number")
    if not isinstance(number, int):
        return None

    title = _text(payload.get("title"))
    if not title:
        return None

    return ProjectListing(number=number, title=title)


def _field_value(fields: list[object], name: str) -> object:
    for field in fields:
        if is_json_object(field) and field.get("name") == name:
            return field.get("value")
    return None


def _option_name(value: object) -> object:
    """A single-select field's chosen option.

    The value arrives as an object carrying `name`, unlike every other name in this API. The
    OpenAPI description leaves a field value untyped, so a bare string is taken as the option
    itself rather than refused.
    """
    if is_json_object(value):
        return value.get("name")
    return value if isinstance(value, str) else None


def _iteration_title(value: object) -> str | None:
    """An iteration field's name, which is the one field shape that is nobody else's.

    GitHub sends the start date, the duration and whether it has finished alongside the name, under
    a `title` that is a `{raw, html}` pair like every other name in this API. None of the rest is
    what a one-line row in a thread wants, so only the name comes out.
    """
    if is_json_object(value):
        return _text(value.get("title"))
    return None


def _text(value: object) -> str | None:
    """The plain form of one of GitHub's `{raw, html}` pairs, or a bare string if it is one."""
    if isinstance(value, str):
        return value.strip() or None
    if is_json_object(value):
        raw = value.get("raw")
        if isinstance(raw, str):
            return raw.strip() or None
    return None


def _board_url(payload: JsonObject, project_number: int) -> str:
    """The board's own page, which is the nearest true link a draft card has.

    Both halves come out of the project's API url, which is the only place a card carries
    either. The kind of owner is read rather than assumed because it is part of the path: an
    organisation's board is `/orgs/`, and the `/users/` form of the same board is not a slower
    route to it but a page that does not exist. A link is worse than no link when it is wrong,
    and this one is written into a thread's metadata and kept.
    """
    url = payload.get("project_url")
    if isinstance(url, str):
        for kind in ("orgs", "users"):
            marker = f"/{kind}/"
            if marker in url:
                owner = url.split(marker, 1)[1].split("/", 1)[0]
                return f"https://github.com/{kind}/{owner}/projects/{project_number}"
    return f"https://github.com/users/unknown/projects/{project_number}"
