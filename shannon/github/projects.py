"""Reading a GitHub project board over REST.

Projects v2 was GraphQL only until GitHub shipped REST for it in September 2025, and REST is what
this uses: the rest of this bot speaks REST, and a second transport for one feature would be a
second set of failure modes to understand. The endpoints are the user-owned ones, because a
personal account is what this runs against, and organisation boards answer the same shape under a
different prefix.

Two calls per read, plus a page. A board's items come back carrying only their Title unless the
request names which fields it wants, and the names are integer ids that have to be looked up
first. The ids are per board and change only when somebody edits its columns, so they are fetched
once and kept.

Everything here checks the shape it was given rather than trusting it. That is the house style
for foreign JSON, and it earns its keep on an API that was in preview last year: a field that
arrives in a shape nobody expected leaves one card unread instead of ending the poll.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from shannon.domain.enums import ObjectType
from shannon.github import mapping

logger = logging.getLogger(__name__)


# The board column lives in the single-select field GitHub's own templates call Status. A board
# that renamed it is a board this cannot read, which is worth saying out loud rather than
# guessing at whichever single-select field happens to come first.
STATUS_FIELD = "Status"
TITLE_FIELD = "Title"

# GitHub's maximum. A board is read whole every time, so round trips are the thing to minimise.
PAGE_SIZE = 100

# What GitHub calls the thing a card wraps, mapped to what this bot calls it. A draft is a
# ticket; the other two are already mirrored from their own webhooks and are looked up rather
# than created.
CONTENT_TYPES: dict[str, ObjectType] = {
    "DraftIssue": ObjectType.TICKET,
    "Issue": ObjectType.ISSUE,
    "PullRequest": ObjectType.PR,
}


@dataclass(frozen=True, slots=True)
class BoardItem:
    """One card on a board, as this service needs it.

    Not GitHub's response shape. The client turns whatever GitHub sends into this, so a change
    to their JSON is a change to one parser rather than to any of the logic below.
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

    @property
    def is_draft(self) -> bool:
        return self.kind is ObjectType.TICKET


class ReadsJson(Protocol):
    """Fetching JSON, one body or a page at a time.

    Declared here rather than importing the GitHub client, so that reading a board depends on
    something that answers with JSON rather than on everything that talks to GitHub.
    """

    async def get_json(self, path: str, **params: Any) -> Any: ...

    def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]: ...


class HttpProjectBoards:
    """`ReadsBoards` on top of GitHub's REST API for user-owned projects."""

    def __init__(self, client: ReadsJson) -> None:
        self._client = client
        self._fields: dict[tuple[str, int], tuple[int, ...]] = {}

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        """Every card on the board, as far as this bot is concerned.

        Archived cards are dropped. Archiving is how somebody takes a card off the board without
        deleting it, so mirroring one would put back a thread for work already put away.
        """
        wanted = await self._field_ids(owner, project_number)
        params: dict[str, Any] = {"per_page": PAGE_SIZE}
        if wanted:
            params["fields"] = ",".join(str(field) for field in wanted)

        items: list[BoardItem] = []
        async for body in self._client.get_pages(
            f"/users/{owner}/projectsV2/{project_number}/items", **params
        ):
            rows = body if isinstance(body, list) else []
            items.extend(
                item for row in rows if (item := parse_item(row, project_number)) is not None
            )
        return items

    async def _field_ids(self, owner: str, project_number: int) -> tuple[int, ...]:
        """The ids of the Title and Status fields, looked up once per board.

        An answer with no Status in it is not remembered, whatever else it carried. Without
        that id the request does not ask for the field, GitHub does not send it, and every card
        comes back with no column at all. It used to be remembered as long as Title was there,
        so a board whose Status somebody renamed was read that way for the life of the process
        rather than for one poll. The answer is a board somebody has to fix or a response that
        arrived wrong, the next read may well get right, and there is no telling the two apart
        from here.
        """
        key = (owner, project_number)
        if key in self._fields:
            return self._fields[key]

        body = await self._client.get_json(f"/users/{owner}/projectsV2/{project_number}/fields")
        rows = body if isinstance(body, list) else []
        by_name = {
            row.get("name"): field_id
            for row in rows
            if isinstance(row, Mapping)
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


def parse_item(payload: Any, project_number: int) -> BoardItem | None:
    """One card, or None for one this bot cannot make sense of.

    Every kind of card is read, not only drafts. A card wrapping an issue or a pull request is
    what "mirror project board movement" mostly means in practice, and the poller uses the
    content id to find the thread that issue already has rather than opening a second one.
    """
    if not isinstance(payload, Mapping):
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

    content = payload.get("content")
    content = content if isinstance(content, Mapping) else {}

    fields = payload.get("fields")
    fields = fields if isinstance(fields, list) else []

    title = _text(_field_value(fields, TITLE_FIELD)) or _text(content.get("title"))
    if not title:
        return None

    return BoardItem(
        item_id=item_id,
        kind=kind,
        title=title,
        column=_text(_option_name(_field_value(fields, STATUS_FIELD))),
        # A draft has no page of its own, so the board is the nearest true link. An issue or a
        # pull request has one, and its own thread already shows it.
        html_url=_text(content.get("html_url"))
        or f"https://github.com/users/{_owner_of(payload)}/projects/{project_number}",
        updated_at=mapping.parse_timestamp(payload.get("updated_at")),
        # What the card wraps, by GitHub's id for it, which is the same id the tracked item was
        # stored under when its own webhook arrived. None for a draft, which wraps nothing.
        content_id=content.get("id") if isinstance(content.get("id"), int) else None,
    )


def _field_value(fields: list[Any], name: str) -> Any:
    for field in fields:
        if isinstance(field, Mapping) and field.get("name") == name:
            return field.get("value")
    return None


def _option_name(value: Any) -> Any:
    """A single-select field's chosen option.

    `value.name` is an object rather than a string here, unlike every other name in this API,
    which is the kind of thing that reads fine and silently gives every card no column at all.

    A value that is already a bare string is taken as the option itself. The OpenAPI description
    leaves a field value untyped, so the nesting is documented by one example, and refusing the
    flat form would turn a shape nobody promised against us into a board with no columns.
    """
    if isinstance(value, Mapping):
        return value.get("name")
    return value if isinstance(value, str) else None


def _text(value: Any) -> str | None:
    """The plain form of one of GitHub's `{raw, html}` pairs, or a bare string if it is one.

    Both, because the OpenAPI description leaves a field's value untyped, so the nesting is
    documented by example only. Taking either shape costs one branch and removes the one way
    this could read every card as blank.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        if isinstance(raw, str):
            return raw.strip() or None
    return None


def _owner_of(payload: Mapping[str, Any]) -> str:
    """The login out of the project's API url, which is the only place a card carries it."""
    url = payload.get("project_url")
    if isinstance(url, str) and "/users/" in url:
        return url.split("/users/", 1)[1].split("/", 1)[0]
    return "unknown"
