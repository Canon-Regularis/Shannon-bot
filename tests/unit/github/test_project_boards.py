"""Turning GitHub's project board JSON into cards this bot can mirror.

The shapes here come from GitHub's published REST documentation for user-owned Projects v2, not
from a live board: the token this was built with cannot read projects. Every field is checked
before it is read for that reason, so a shape that turns out to differ costs one unread card
rather than a poll that dies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from shannon.domain.enums import ObjectType
from shannon.github.projects import HttpProjectBoards, parse_item

PROJECT = 3

# What an issue card carries in place of a draft's bare title.
WRAPPED = {
    "id": 2807646438,
    "number": 1093,
    "title": "Code scanning: status at the org level",
    "html_url": "https://github.com/monalisa/hello-world/issues/1093",
    "state": "open",
}


def draft(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 74106766,
        "node_id": "PVTI_lADNJr_OAJfQ484EaseO",
        "project_url": f"https://api.github.com/users/monalisa/projectsV2/{PROJECT}",
        "content_type": "DraftIssue",
        "archived_at": None,
        "updated_at": "2026-08-17T19:08:55Z",
        "content": {"title": "Write the migration runbook", "body": None},
        "fields": [
            {
                "id": 39516,
                "name": "Title",
                "data_type": "title",
                "value": {"raw": "Write the migration runbook", "html": "Write the runbook"},
            },
            {
                "id": 39518,
                "name": "Status",
                "data_type": "single_select",
                "value": {
                    "id": "0b6e37be",
                    "name": {"raw": "In Progress", "html": "In Progress"},
                    "color": "GRAY",
                },
            },
        ],
    }
    payload.update(overrides)
    return payload


class FakeJson:
    """An HTTP client that answers each path with whatever it was given.

    `get_pages` is a generator because the real one is: the project endpoints paginate by a
    cursor in the Link header, so the client follows it rather than counting pages, and a fake
    that answered with a plain list would let a caller that still counted pages pass.
    """

    def __init__(self, pages: list[Any] | None = None, **bodies: Any) -> None:
        self.bodies = bodies
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, path: str, **params: Any) -> Any:
        self.calls.append((path, params))
        return self.bodies.get("fields" if path.endswith("/fields") else "items", [])

    async def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]:
        self.calls.append((path, params))
        for page in self.pages if self.pages is not None else [self.bodies.get("items", [])]:
            yield page


class TestReadingOneCard:
    def test_a_draft_card_becomes_a_ticket(self) -> None:
        item = parse_item(draft(), PROJECT)

        assert item is not None
        assert item.item_id == 74106766
        assert item.title == "Write the migration runbook"
        assert item.column == "In Progress"

    def test_the_column_is_read_out_of_the_nested_name(self) -> None:
        """A single-select value's `name` is an object here, unlike every other name in this
        API. Read as a string it comes back None and every card looks statusless."""
        assert parse_item(draft(), PROJECT).column == "In Progress"

    def test_a_card_links_to_the_board_it_lives_on(self) -> None:
        """A draft has no page of its own, so the board is the nearest true link."""
        item = parse_item(draft(), PROJECT)

        assert item.html_url == f"https://github.com/users/monalisa/projects/{PROJECT}"

    def test_a_card_with_no_status_set_has_no_column(self) -> None:
        item = parse_item(draft(fields=[]), PROJECT)

        assert item is not None
        assert item.column is None
        assert item.title == "Write the migration runbook", "it lost the title with the fields"


class TestCardsThatWrapSomethingElse:
    """Not skipped. A card wrapping an issue is most of what a board actually holds, and moving
    it is most of what "mirror board movement" means; the poller uses the content id to find the
    thread that issue already has rather than opening a second one."""

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [("Issue", ObjectType.ISSUE), ("PullRequest", ObjectType.PR)],
    )
    def test_it_is_read_and_says_what_it_wraps(
        self, content_type: str, expected: ObjectType
    ) -> None:
        item = parse_item(draft(content_type=content_type, content=WRAPPED), PROJECT)

        assert item is not None
        assert item.kind is expected
        assert item.is_draft is False

    def test_it_carries_the_id_the_wrapped_item_was_stored_under(self) -> None:
        """The same id its own webhook arrived with, which is how the tracked row is found."""
        item = parse_item(draft(content_type="Issue", content=WRAPPED), PROJECT)

        assert item.content_id == 2807646438

    def test_it_links_to_the_item_rather_than_to_the_board(self) -> None:
        """Unlike a draft, an issue has a page of its own worth pointing at."""
        item = parse_item(draft(content_type="Issue", content=WRAPPED), PROJECT)

        assert item.html_url == "https://github.com/monalisa/hello-world/issues/1093"

    def test_a_draft_wraps_nothing(self) -> None:
        item = parse_item(draft(), PROJECT)

        assert item.kind is ObjectType.TICKET
        assert item.is_draft is True
        assert item.content_id is None

    def test_a_kind_of_card_nobody_has_taught_us_is_skipped(self) -> None:
        assert parse_item(draft(content_type="Redacted"), PROJECT) is None


class TestWhatIsNotMirrored:
    def test_an_archived_card_is_skipped(self) -> None:
        """Archiving is how somebody takes a card off a board without deleting it, so putting
        its thread back would be undoing that."""
        assert parse_item(draft(archived_at="2026-01-01T00:00:00Z"), PROJECT) is None

    def test_a_card_with_no_title_anywhere_is_skipped(self) -> None:
        assert parse_item(draft(fields=[], content={}), PROJECT) is None

    @pytest.mark.parametrize("payload", [None, "a string", [], 7])
    def test_anything_that_is_not_a_card_is_skipped(self, payload: Any) -> None:
        assert parse_item(payload, PROJECT) is None

    def test_a_card_with_an_unreadable_id_is_skipped(self) -> None:
        assert parse_item(draft(id="PVTI_notanumber"), PROJECT) is None


class TestReadingABoard:
    async def test_it_asks_for_the_fields_it_needs_by_id(self) -> None:
        """Items come back carrying only their title unless the request names the fields, and
        the names are integer ids that have to be looked up first."""
        client = FakeJson(
            fields=[{"id": 39516, "name": "Title"}, {"id": 39518, "name": "Status"}],
            items=[draft()],
        )

        items = await HttpProjectBoards(client).list_board_items("monalisa", PROJECT)

        assert len(items) == 1
        assert client.calls[0][0] == f"/users/monalisa/projectsV2/{PROJECT}/fields"
        assert client.calls[1][1]["fields"] == "39516,39518"
        assert client.calls[1][1]["per_page"] == 100
        assert "page" not in client.calls[1][1], "it counted pages instead of following the cursor"

    async def test_the_field_ids_are_looked_up_once_and_kept(self) -> None:
        """They change only when somebody edits the board's columns, and this runs every minute."""
        client = FakeJson(fields=[{"id": 39518, "name": "Status"}], items=[draft()])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)
        await boards.list_board_items("monalisa", PROJECT)

        assert sum(path.endswith("/fields") for path, _ in client.calls) == 1

    async def test_a_board_with_no_status_field_still_reads(self) -> None:
        """Worth a warning rather than a failure: the cards are real, they just have no column,
        and a board whose Status was renamed should not stop the whole mirror."""
        client = FakeJson(fields=[{"id": 1, "name": "Title"}], items=[draft(fields=[])])

        items = await HttpProjectBoards(client).list_board_items("monalisa", PROJECT)

        assert [item.column for item in items] == [None]

    async def test_an_answer_that_is_not_a_list_reads_as_an_empty_board(self) -> None:
        client = FakeJson(fields={"message": "Not Found"}, items={"message": "Not Found"})

        assert await HttpProjectBoards(client).list_board_items("monalisa", PROJECT) == []


class TestFieldsInShapesNobodyPromised:
    """The OpenAPI description leaves a field value untyped, so every shape is possible.

    These are the ones that would otherwise read as a wrong value rather than as no value, which
    is the difference between one blank card and a board that quietly says the wrong thing.
    """

    def test_an_option_whose_raw_is_not_a_string_is_no_column(self) -> None:
        """A Number field's value is documented nowhere. Reading `raw` blindly would put an int
        where a column name goes, and status_from_column would never match it again.

        Nested under `name` on purpose: a value with no `name` at all never reaches the text
        reader, so a test that left it out would pass without exercising this.
        """
        item = parse_item(
            draft(fields=[{"name": "Status", "value": {"name": {"raw": 7, "html": "7"}}}]),
            PROJECT,
        )

        assert item is not None
        assert item.column is None

    def test_a_title_whose_raw_is_not_a_string_falls_through_to_the_content(self) -> None:
        """The same reader, on the field that decides whether a card is mirrored at all."""
        item = parse_item(draft(fields=[{"name": "Title", "value": {"raw": 7}}]), PROJECT)

        assert item is not None
        assert item.title == "Write the migration runbook", "it took a number for a title"

    @pytest.mark.parametrize("value", [7, [], {"html": "In Progress"}, None])
    def test_any_other_shape_reads_as_no_value(self, value: Any) -> None:
        item = parse_item(draft(fields=[{"name": "Status", "value": value}]), PROJECT)

        assert item.column is None

    def test_a_bare_string_value_is_taken_as_it_is(self) -> None:
        """The nesting is documented by example only, so the flat form has to work too."""
        item = parse_item(draft(fields=[{"name": "Status", "value": "Done"}]), PROJECT)

        assert item.column == "Done"

    def test_a_card_with_no_project_url_still_gets_a_link(self) -> None:
        """The owner is only carried in that URL. Without it the link cannot name anybody, and a
        card with no link at all would be worse than one pointing at the wrong board."""
        item = parse_item(draft(project_url=None), PROJECT)

        assert item is not None
        assert item.html_url == f"https://github.com/users/unknown/projects/{PROJECT}"

    @pytest.mark.parametrize("stamp", ["not a date", "2026-13-45T99:00:00Z", "", None, 7])
    def test_a_timestamp_it_cannot_read_is_no_timestamp(self, stamp: Any) -> None:
        """A card with no readable timestamp is always synced, which is a wasted edit rather
        than a card that never updates again."""
        item = parse_item(draft(updated_at=stamp), PROJECT)

        assert item is not None
        assert item.updated_at is None


@pytest.mark.parametrize("content_type", [["DraftIssue"], {"a": 1}, 7, None])
def test_a_content_type_that_is_not_a_string_is_skipped(content_type: Any) -> None:
    """A dict lookup on an unhashable key raises rather than answering None, and one malformed
    card would have ended the whole poll rather than being passed over."""
    assert parse_item(draft(content_type=content_type), PROJECT) is None
