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
from shannon.domain.priority import parse_priority
from shannon.github import mapping
from shannon.github.errors import GitHubAuthError, GitHubNotFoundError

logger = logging.getLogger(__name__)


# The board column lives in the single-select field GitHub's own templates call Status. A board
# that renamed it cannot be read.
STATUS_FIELD = "Status"
TITLE_FIELD = "Title"
# Optional, unlike the two above. GitHub's default template does not ship one, and a board
# without it mirrors perfectly well - it simply cannot be told a priority.
PRIORITY_FIELD = "Priority"

# GitHub's maximum.
PAGE_SIZE = 100

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

    # What `list_board_items` sends as `fields=`. Title and Status only: it governs the
    # READ, and nothing reads a card's priority back, so naming Priority here would send a
    # field per card per poll that nothing parses.
    wanted: tuple[int, ...]
    status: BoardSelect
    priority: BoardSelect | None = None


@dataclass(frozen=True, slots=True)
class BoardItem:
    """One card on a board, as this service needs it."""

    item_id: int
    title: str
    column: str | None
    html_url: str
    kind: ObjectType = ObjectType.TICKET
    updated_at: datetime | None = None
    # GitHub's id for the issue or pull request the card wraps, which is the id that item was
    # already stored under when its own webhook arrived. None for a draft, which wraps nothing.
    content_id: int | None = None

    @property
    def is_draft(self) -> bool:
        return self.kind is ObjectType.TICKET


class WritesJson(Protocol):
    """Sending JSON at a path, which is all a board write needs.

    Its own Protocol beside the reader rather than a method on `GitHubClient`. That one carries
    `get_json` only because the wiring hands the same object to the board reader when no project
    token is set; putting a PATCH there would hand every service in this project the ability to
    write to any path with an installation token, which is strictly wider than anything else on
    it and is exactly what the module docstring says a handle must not be.
    """

    async def patch_json(self, path: str, *, owner: str, json: JsonObject) -> None: ...


class ReadsJson(Protocol):
    """Fetching JSON, one body or a page at a time."""

    async def get_json(self, path: str, *, owner: str = "", **params: str | int) -> object: ...

    def get_pages(
        self, path: str, *, owner: str = "", **params: str | int
    ) -> AsyncIterator[object]: ...


class HttpProjectBoards:
    """`ReadsBoards` on top of GitHub's REST API for project boards."""

    def __init__(self, client: ReadsJson, *, writer: WritesJson | None = None) -> None:
        self._client = client
        # None where this deployment has no project token. The reader falls back to the App
        # client, which holds no Projects permission at all, so there is nothing to fall back
        # to for a write - and leaving it None makes "no token means no board writes" a fact of
        # the wiring rather than a check somebody could forget.
        self._writer = writer
        self._fields: dict[tuple[str, int], BoardFields] = {}
        self._kinds: dict[str, str] = {}
        # Boards already complained about for one state, so the complaint is said once
        # rather than once per command. Keyed on the state too: a board with no column for
        # one status very likely has one for another, and keying on the board alone would
        # swallow a different complaint. Never expires, and needs no expiry - the moment
        # somebody adds the column the picker finds it and this is never reached again.
        self._warned: set[tuple[str, int, str]] = set()

    async def list_boards(self, owner: str) -> Sequence[ProjectListing]:
        """Every board this owner has, for somebody choosing one.

        Over the same prefix the reads use, so the organisation-or-person decision is made once
        and a picker cannot offer a board the poller then cannot open.
        """
        kind = await self._owner_kind(owner)
        listings: list[ProjectListing] = []
        async for body in self._client.get_pages(
            f"/{kind}/{quote(owner, safe='')}/projectsV2", owner=owner, per_page=PAGE_SIZE
        ):
            rows = body if is_json_list(body) else []
            listings.extend(listing for row in rows if (listing := parse_listing(row)) is not None)
        return listings

    async def get_board(self, owner: str, project_number: int) -> ProjectListing | None:
        """One board, or None where this token cannot open it.

        Exists so that "the token cannot see this board" becomes a refusal the person who typed
        it reads, rather than a warning once a minute in a log nobody is watching. A picker's
        suggestions are only suggestions - discord.py says so - so the number that arrives here
        may never have been offered.
        """
        board = await self._board_path(owner, project_number)
        try:
            body = await self._client.get_json(board, owner=owner)
        except (GitHubNotFoundError, GitHubAuthError):
            # Folded into the answer this already gives for an unusable board, because an
            # operator cannot act on the difference and the caller has one sentence to say.
            # A board GitHub does not have, a token that may not see it, and a token that is
            # not there at all are 404, 403 and 403 - and GitHub's own words for those name
            # neither the board nor the credential, which is every question worth asking.
            return None
        return parse_listing(body)

    async def move_card(
        self, *, owner: str, project_number: int, card_id: int, state: Status | Priority
    ) -> CardMoved:
        """Set the card's Status or Priority to match, saying what became of it.

        One method over the union rather than two, because the CALLEE picks the field: a
        caller physically cannot hand a priority to the Status field. `spoken` is already
        typed over the same union and `_refuse_a_name_this_bot_owns` already branches on it.

        An answer rather than a bool, because the ways of declining are not alike and one of
        them is worth telling a person about. No token and an unreadable Status field are
        both invisible on the board and identical for every command; a board with no column
        for this state is permanent until somebody edits it AND visible, because the person
        will open the board and find the card where it was.

        UNSET never arrives. The picker offers three priorities and `priority_change` raises
        on a fourth long before this, so a guard here would be an arm no caller can take -
        which matters more than it sounds, since `spoken(Priority.UNSET)` is "None" and
        would match a board column literally called None.
        """
        if self._writer is None:
            return CardMoved(CardMove.NO_WRITER)

        board = await self._board_path(owner, project_number)
        fields = await self._board_fields(board, owner, project_number)
        if fields is None:
            return CardMoved(CardMove.UNREADABLE)

        select = fields.status if isinstance(state, Status) else fields.priority
        if select is None:
            return CardMoved(CardMove.NO_FIELD)

        option = _option_for(select.options, state)
        if option is None:
            self._complain(owner, project_number, state, card_id)
            return CardMoved(CardMove.NO_COLUMN)

        await self._writer.patch_json(
            f"{board}/items/{card_id}",
            owner=owner,
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
        """Every card on the board, archived ones dropped.

        Archiving is how a card is taken off the board without deleting it, so mirroring one
        would put back a thread for work already put away.
        """
        board = await self._board_path(owner, project_number)
        fields = await self._board_fields(board, owner, project_number)
        params: dict[str, str | int] = {"per_page": PAGE_SIZE}
        if fields is not None:
            params["fields"] = ",".join(str(field) for field in fields.wanted)

        items: list[BoardItem] = []
        async for body in self._client.get_pages(f"{board}/items", owner=owner, **params):
            rows = body if is_json_list(body) else []
            items.extend(
                item for row in rows if (item := parse_item(row, project_number)) is not None
            )
        return items

    async def _board_path(self, owner: str, project_number: int) -> str:
        """Where this board lives, which the kind of account owning it decides.

        Worked out once per read and handed down, rather than rebuilt by each caller that wants
        it: a second call would hit the cache below and cover its branch incidentally, leaving
        the test that the kind is asked for only once proving nothing.
        """
        kind = await self._owner_kind(owner)
        return f"/{kind}/{quote(owner, safe='')}/projectsV2/{project_number}"

    async def _owner_kind(self, owner: str) -> str:
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

        body = await self._client.get_json(f"/users/{quote(owner, safe='')}", owner=owner)
        account: JsonObject = body if is_json_object(body) else {}
        kind = account.get("type")
        if not isinstance(kind, str):
            logger.warning(
                "GitHub did not say what kind of account %r is, so its board is read as a "
                "person's; an organisation's board will answer 404 until it does",
                owner,
            )
            return "users"

        decided = "orgs" if kind == "Organization" else "users"
        self._kinds[owner] = decided
        return decided

    async def _board_fields(
        self, board: str, owner: str, project_number: int
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

        body = await self._client.get_json(f"{board}/fields", owner=owner)
        rows = body if is_json_list(body) else []
        named = {
            row.get("name"): row
            for row in rows
            if is_json_object(row)
            and row.get("name") in (TITLE_FIELD, STATUS_FIELD, PRIORITY_FIELD)
            and isinstance(row.get("id"), int)
        }
        status = named.get(STATUS_FIELD)
        if status is None:
            logger.warning(
                "project %s answered with no %r field, so no card can carry a status",
                project_number,
                STATUS_FIELD,
            )
            return None

        status_id = status["id"]
        assert isinstance(status_id, int)
        found = BoardFields(
            wanted=tuple(
                field_id
                for name in (TITLE_FIELD, STATUS_FIELD)
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
    """
    if not is_json_object(payload):
        return None
    if payload.get("archived_at") is not None:
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
