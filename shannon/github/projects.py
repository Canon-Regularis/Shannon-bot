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
from typing import Protocol
from urllib.parse import quote

from shannon.domain.enums import ObjectType
from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.github import mapping

logger = logging.getLogger(__name__)


# The board column lives in the single-select field GitHub's own templates call Status. A board
# that renamed it cannot be read.
STATUS_FIELD = "Status"
TITLE_FIELD = "Title"

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


class ReadsJson(Protocol):
    """Fetching JSON, one body or a page at a time."""

    async def get_json(self, path: str, *, owner: str = "", **params: str | int) -> object: ...

    def get_pages(
        self, path: str, *, owner: str = "", **params: str | int
    ) -> AsyncIterator[object]: ...


class HttpProjectBoards:
    """`ReadsBoards` on top of GitHub's REST API for project boards."""

    def __init__(self, client: ReadsJson) -> None:
        self._client = client
        self._fields: dict[tuple[str, int], tuple[int, ...]] = {}
        self._kinds: dict[str, str] = {}

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
        return parse_listing(await self._client.get_json(board, owner=owner))

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        """Every card on the board, archived ones dropped.

        Archiving is how a card is taken off the board without deleting it, so mirroring one
        would put back a thread for work already put away.
        """
        board = await self._board_path(owner, project_number)
        wanted = await self._field_ids(board, owner, project_number)
        params: dict[str, str | int] = {"per_page": PAGE_SIZE}
        if wanted:
            params["fields"] = ",".join(str(field) for field in wanted)

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

    async def _field_ids(self, board: str, owner: str, project_number: int) -> tuple[int, ...]:
        """The ids of the Title and Status fields, looked up once per board.

        Items come back carrying only their Title unless the request names the field ids it
        wants. An answer with no Status in it is not remembered: without that id every card
        reads as having no column, and the next read may get an answer that has it.
        """
        key = (owner, project_number)
        if key in self._fields:
            return self._fields[key]

        body = await self._client.get_json(f"{board}/fields", owner=owner)
        rows = body if is_json_list(body) else []
        by_name = {
            row.get("name"): field_id
            for row in rows
            if is_json_object(row)
            and row.get("name") in (TITLE_FIELD, STATUS_FIELD)
            and isinstance(field_id := row.get("id"), int)
        }
        found = tuple(by_name[name] for name in (TITLE_FIELD, STATUS_FIELD) if name in by_name)
        if STATUS_FIELD not in by_name:
            logger.warning(
                "project %s answered with no %r field, so no card can carry a status",
                project_number,
                STATUS_FIELD,
            )
            return found

        self._fields[key] = found
        return found


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
