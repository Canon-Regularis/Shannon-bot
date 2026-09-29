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

from shannon.domain.enums import ObjectType, Priority, Status
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
)
from shannon.github.projects import CardMove, HttpProjectBoards, parse_item

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
        if path.endswith("/fields"):
            return self.bodies.get("fields", [])
        if "/projectsV2/" in path:
            # One board, opened by number. `list_board_items` pages instead, so nothing else
            # reaches this by that path.
            return self.bodies.get("one_board", {})
        if path.endswith("/projectsV2"):
            return self.bodies.get("boards", [])
        # The account lookup that decides the prefix. A person by default, which is the prefix
        # every board was read under before there was a choice.
        return self.bodies.get("account", {"type": "User"})

    async def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]:
        self.calls.append((path, params))
        if path.endswith("/projectsV2"):
            yield self.bodies.get("boards", [])
            return
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
        assert f"/users/monalisa/projectsV2/{PROJECT}/fields" in [path for path, _ in client.calls]
        listed = next(params for path, params in client.calls if path.endswith("/items"))
        assert listed["fields"] == "39516,39518"
        assert listed["per_page"] == 100
        assert "page" not in listed, "it counted pages instead of following the cursor"

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

    async def test_an_answer_with_no_status_field_is_asked_again_next_time(self) -> None:
        """The ids are looked up once and kept, which is right for an answer worth keeping.

        Title alone is not one. Without the Status id the request never asks for the field, so
        every card comes back with no column, and remembering that answer read the board that
        way for the life of the process rather than for one poll. A renamed field is one edit to
        undo, and the poll after it should see the board again.
        """
        client = FakeJson(fields=[{"id": 1, "name": "Title"}], items=[draft(fields=[])])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)
        await boards.list_board_items("monalisa", PROJECT)

        assert sum(path.endswith("/fields") for path, _ in client.calls) == 2

    async def test_an_answer_that_is_not_a_list_reads_as_an_empty_board(self) -> None:
        client = FakeJson(fields={"message": "Not Found"}, items={"message": "Not Found"})

        assert await HttpProjectBoards(client).list_board_items("monalisa", PROJECT) == []


class TestListingTheBoardsAnOwnerHas:
    """What `/set_board` offers. Over the same prefix the reads use, deliberately: a picker on a
    path of its own could offer a board the poller then cannot open."""

    async def test_it_lists_them(self) -> None:
        client = FakeJson(
            boards=[
                {"number": 3, "title": "Roadmap"},
                {"number": 7, "title": {"raw": "Bugs", "html": "Bugs"}},
            ]
        )

        listed = await HttpProjectBoards(client).list_boards("monalisa")

        assert [(one.number, one.title) for one in listed] == [(3, "Roadmap"), (7, "Bugs")]

    async def test_an_organisation_is_listed_under_orgs(self) -> None:
        client = FakeJson(account={"type": "Organization"}, boards=[{"number": 3, "title": "R"}])

        await HttpProjectBoards(client).list_boards("acme")

        assert any(path == "/orgs/acme/projectsV2" for path, _ in client.calls)

    @pytest.mark.parametrize(
        "row",
        [
            {"number": 3},
            {"title": "Roadmap"},
            {"number": "3", "title": "Roadmap"},
            {"number": 3, "title": ""},
            {"number": 3, "title": {"html": "Roadmap"}},
            "not an object",
            None,
        ],
    )
    async def test_a_row_it_cannot_use_is_passed_over(self, row: Any) -> None:
        """A board with no readable title is not refused for tidiness: the title is the whole of
        what a picker shows, and an entry reading `#7` with nothing beside it is a choice nobody
        can make."""
        client = FakeJson(boards=[row, {"number": 9, "title": "Real"}])

        listed = await HttpProjectBoards(client).list_boards("monalisa")

        assert [one.number for one in listed] == [9]

    async def test_an_answer_that_is_not_a_list_is_no_boards(self) -> None:
        client = FakeJson(boards={"message": "Not Found"})

        assert await HttpProjectBoards(client).list_boards("monalisa") == []


class TestOpeningOneBoard:
    """So that "this token cannot see that board" is a sentence the person who typed it reads,
    rather than a warning once a minute in a log nobody is watching."""

    async def test_it_answers_the_board(self) -> None:
        client = FakeJson(one_board={"number": 3, "title": "Roadmap"})

        found = await HttpProjectBoards(client).get_board("monalisa", PROJECT)

        assert found is not None
        assert (found.number, found.title) == (3, "Roadmap")

    async def test_it_asks_under_the_owners_own_kind(self) -> None:
        client = FakeJson(account={"type": "Organization"}, one_board={"number": 3, "title": "R"})

        await HttpProjectBoards(client).get_board("acme", PROJECT)

        assert any(path == f"/orgs/acme/projectsV2/{PROJECT}" for path, _ in client.calls)

    async def test_a_body_it_cannot_read_is_no_board(self) -> None:
        client = FakeJson(one_board={"message": "Not Found"})

        assert await HttpProjectBoards(client).get_board("monalisa", PROJECT) is None


class TestWhichKindOfAccountOwnsTheBoard:
    """A login names one account of one kind: GitHub keeps users and organisations in a single
    namespace, so `acme` cannot be both. The wrong prefix is therefore not a slower way to the
    same board, it is a 404 — which is the answer every organisation's board gave here until the
    kind was asked about rather than assumed.
    """

    async def test_an_organisations_board_is_read_under_orgs(self) -> None:
        client = FakeJson(account={"type": "Organization"}, items=[draft()])

        await HttpProjectBoards(client).list_board_items("acme", PROJECT)

        boards = [path for path, _ in client.calls if "/projectsV2/" in path]
        assert boards
        assert all(path.startswith(f"/orgs/acme/projectsV2/{PROJECT}") for path in boards)

    async def test_a_persons_board_is_still_read_under_users(self) -> None:
        client = FakeJson(account={"type": "User"}, items=[draft()])

        await HttpProjectBoards(client).list_board_items("monalisa", PROJECT)

        assert any(
            path.startswith(f"/users/monalisa/projectsV2/{PROJECT}") for path, _ in client.calls
        )

    async def test_the_kind_is_asked_once_and_kept(self) -> None:
        """It changes when an account is converted, which is not a thing that happens between
        two polls a minute apart, and this would otherwise be a second request every time."""
        client = FakeJson(account={"type": "Organization"}, items=[draft()])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("acme", PROJECT)
        await boards.list_board_items("acme", PROJECT)

        assert sum(path == "/users/acme" for path, _ in client.calls) == 1

    async def test_the_lookup_carries_the_boards_own_credential(self) -> None:
        """The board is read with a token of its own, and an organisation can be private. Asked
        anonymously this is both a possible 404 and a share of an IP-wide hourly allowance."""
        client = FakeJson(account={"type": "Organization"}, items=[draft()])

        await HttpProjectBoards(client).list_board_items("acme", PROJECT)

        asked = next(params for path, params in client.calls if path == "/users/acme")
        assert asked["owner"] == "acme"

    async def test_the_owner_is_escaped_into_the_path(self) -> None:
        """It reaches here from a setting somebody typed, and every other interpolation in this
        client is quoted."""
        client = FakeJson(account={"type": "Organization"}, items=[])

        await HttpProjectBoards(client).list_board_items("a b/c", PROJECT)

        assert all("a b/c" not in path for path, _ in client.calls if "projectsV2" in path)
        assert any("a%20b%2Fc" in path for path, _ in client.calls)

    @pytest.mark.parametrize("account", [{"type": 7}, {}, ["not an object"], None])
    async def test_an_answer_it_cannot_read_is_taken_as_a_person(
        self, account: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Which is how every board was read before there was a choice, so an unreadable answer
        costs an organisation what it already had rather than breaking a working board."""
        client = FakeJson(account=account, items=[draft()])

        with caplog.at_level("WARNING"):
            await HttpProjectBoards(client).list_board_items("acme", PROJECT)

        assert any(path.startswith(f"/users/acme/projectsV2/{PROJECT}") for path, _ in client.calls)
        assert "kind of account" in caplog.text

    async def test_an_answer_it_cannot_read_is_asked_again_next_poll(self) -> None:
        """A guess is not an answer worth keeping. Remembered, one blip would decide the prefix
        for the life of the process, and an organisation's board would stay dead until a
        restart nobody knew to do."""
        client = FakeJson(account={}, items=[draft()])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("acme", PROJECT)
        await boards.list_board_items("acme", PROJECT)

        assert sum(path == "/users/acme" for path, _ in client.calls) == 2


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

    def test_a_draft_on_an_organisations_board_links_to_that_board(self) -> None:
        """The kind of owner is half the path, not decoration. `/users/` and `/orgs/` are two
        different pages, and only one of them exists for any given board."""
        item = parse_item(
            draft(project_url=f"https://api.github.com/orgs/acme/projectsV2/{PROJECT}"), PROJECT
        )

        assert item is not None
        assert item.html_url == f"https://github.com/orgs/acme/projects/{PROJECT}"

    def test_a_project_url_naming_neither_kind_falls_back(self) -> None:
        """A shape this bot has not seen. Guessing an owner out of it would put a real login on
        a board it may not own, so it says so instead."""
        item = parse_item(
            draft(project_url=f"https://api.github.com/teams/acme/projectsV2/{PROJECT}"), PROJECT
        )

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


class TestTheStatusFieldsChoices:
    """A write needs the ID of the option it moves a card to, not its name.

    They ride along on the `/fields` answer the reader already makes, so keeping them costs no
    second call. Every row is checked before it is read, like every other shape in this module:
    these came from published documentation rather than from a board.
    """

    def fields(self, status: dict[str, Any]) -> list[Any]:
        return [{"id": 39516, "name": "Title"}, status]

    async def test_the_options_are_kept_with_their_ids(self) -> None:
        client = FakeJson(
            fields=self.fields(
                {
                    "id": 39518,
                    "name": "Status",
                    "options": [
                        {"id": "0b6e37be", "name": {"raw": "Todo", "html": "Todo"}},
                        {"id": "aa1c2d3e", "name": "In Progress"},
                    ],
                }
            ),
            items=[draft()],
        )
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)
        found = boards._fields[("monalisa", PROJECT)]

        assert [(one.option_id, one.name) for one in found.status.options] == [
            ("0b6e37be", "Todo"),
            ("aa1c2d3e", "In Progress"),
        ]

    async def test_the_order_github_gave_them_is_kept(self) -> None:
        """It is the board's own left-to-right order, which is the tiebreak when two columns
        mean the same thing to this bot."""
        client = FakeJson(
            fields=self.fields(
                {
                    "id": 39518,
                    "name": "Status",
                    "options": [
                        {"id": "c", "name": "Done"},
                        {"id": "a", "name": "Todo"},
                        {"id": "b", "name": "In Progress"},
                    ],
                }
            ),
            items=[],
        )
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)

        assert [one.option_id for one in boards._fields[("monalisa", PROJECT)].status.options] == [
            "c",
            "a",
            "b",
        ]

    @pytest.mark.parametrize(
        "row",
        [
            {"name": "Todo"},
            {"id": 7, "name": "Todo"},
            {"id": "a"},
            {"id": "a", "name": ""},
            {"id": "a", "name": {"html": "Todo"}},
            "not an object",
            None,
        ],
    )
    async def test_a_choice_it_cannot_read_is_passed_over(self, row: Any) -> None:
        """An option id is a STRING here while a field id is an int, so an int id is a shape
        this cannot use rather than one to coerce."""
        client = FakeJson(
            fields=self.fields(
                {"id": 39518, "name": "Status", "options": [row, {"id": "ok", "name": "Done"}]}
            ),
            items=[],
        )
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)

        assert [one.option_id for one in boards._fields[("monalisa", PROJECT)].status.options] == [
            "ok"
        ]

    @pytest.mark.parametrize("listed", [None, {}, "nope", 7])
    async def test_a_field_with_no_readable_choices_has_none(self, listed: Any) -> None:
        """A board can be read and mirrored without them. Only a write needs them, and a write
        with nothing to choose from does nothing rather than guessing."""
        status: dict[str, Any] = {"id": 39518, "name": "Status"}
        if listed is not None:
            status["options"] = listed
        client = FakeJson(fields=self.fields(status), items=[])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)

        assert boards._fields[("monalisa", PROJECT)].status.options == ()

    async def test_the_status_id_is_right_on_a_board_with_no_title_field(self) -> None:
        """The bug this shape exists to prevent. `wanted` is built positionally, so it is
        `(status_id,)` here and `(title_id, status_id)` on an ordinary board - a writer reaching
        for `wanted[1]` is wrong on the first and right on the second."""
        client = FakeJson(fields=[{"id": 39518, "name": "Status"}], items=[])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)
        found = boards._fields[("monalisa", PROJECT)]

        assert found.wanted == (39518,)
        assert found.status.field_id == 39518

    async def test_the_status_id_is_right_on_an_ordinary_board(self) -> None:
        client = FakeJson(fields=self.fields({"id": 39518, "name": "Status"}), items=[])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)
        found = boards._fields[("monalisa", PROJECT)]

        assert found.wanted == (39516, 39518)
        assert found.status.field_id == 39518

    async def test_a_board_with_no_status_field_caches_nothing_at_all(self) -> None:
        """One entry holds the ids and the choices together so this rule cannot be honoured for
        one and missed for the other: two caches can disagree about whether a board has a Status
        field, and one cannot."""
        client = FakeJson(fields=[{"id": 1, "name": "Title"}], items=[])
        boards = HttpProjectBoards(client)

        await boards.list_board_items("monalisa", PROJECT)

        assert boards._fields == {}


class FakeWriter:
    """The PATCH half, which is a separate Protocol from the read half on purpose."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str, Any]] = []

    async def patch_json(self, path: str, *, owner: str, json: Any) -> None:
        self.sent.append((path, owner, json))
        if self.error is not None:
            raise self.error


def a_board(**fields: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "id": 39518,
        "name": "Status",
        "options": [
            {"id": "opt-todo", "name": "Todo"},
            {"id": "opt-doing", "name": "In Progress"},
            {"id": "opt-done", "name": "Done"},
        ],
    }
    status.update(fields)
    return status


class TestMovingACard:
    """The write. Its body shape came from published documentation like every other shape in
    this module, so what is pinned here is the decisions around it rather than GitHub's answer:
    which path, which ids, and the three ways it declines to write at all."""

    def boards(self, writer: FakeWriter | None, **bodies: Any) -> HttpProjectBoards:
        client = FakeJson(fields=[{"id": 39516, "name": "Title"}, a_board()], **bodies)
        return HttpProjectBoards(client, writer=writer)

    async def test_it_patches_the_card_with_the_option_id(self) -> None:
        writer = FakeWriter()

        moved = await self.boards(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.IN_REVIEW
        )

        assert moved.outcome is CardMove.MOVED
        assert writer.sent == [
            (
                f"/users/monalisa/projectsV2/{PROJECT}/items/99",
                "monalisa",
                {"fields": [{"id": 39518, "value": "opt-doing"}]},
            )
        ]

    async def test_an_organisations_board_is_written_under_orgs(self) -> None:
        """For the same reason it is read under it: a login names one account of one kind, and
        the wrong prefix is a 404 rather than a longer route."""
        writer = FakeWriter()
        boards = self.boards(writer, account={"type": "Organization"})

        await boards.move_card(owner="acme", project_number=PROJECT, card_id=99, state=Status.DONE)

        assert writer.sent[0][0] == f"/orgs/acme/projectsV2/{PROJECT}/items/99"

    async def test_the_owner_goes_with_it_so_the_write_carries_a_credential(self) -> None:
        """An empty owner sends the PATCH out anonymous, GitHub answers 401, and the poller
        reads that as permanent and writes the card off for good."""
        writer = FakeWriter()

        await self.boards(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
        )

        assert writer.sent[0][1] == "monalisa"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (Status.NOT_REVIEWED, "opt-todo"),
            (Status.IN_REVIEW, "opt-doing"),
            (Status.DONE, "opt-done"),
        ],
    )
    async def test_the_column_is_chosen_through_the_words_a_board_may_use(
        self, status: Status, expected: str
    ) -> None:
        """Read through `status_from_column`, so a board saying Todo or In Progress works
        without an inverse table that could drift from the one this project already keeps."""
        writer = FakeWriter()

        await self.boards(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=status
        )

        assert writer.sent[0][2]["fields"][0]["value"] == expected

    async def test_the_column_named_for_the_state_wins_over_a_synonym(self) -> None:
        """`In Progress` and `In Review` both mean IN_REVIEW. Board order alone would take
        whichever came first, which is how `/status In review` moved a card to In progress.
        The column called what the state is called wins; order only breaks a tie between
        two synonyms, neither of which is the name."""
        writer = FakeWriter()
        client = FakeJson(
            fields=[
                a_board(
                    options=[
                        {"id": "second", "name": "In Review"},
                        {"id": "first", "name": "In Progress"},
                    ]
                )
            ]
        )

        await HttpProjectBoards(client, writer=writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.IN_REVIEW
        )

        assert writer.sent[0][2]["fields"][0]["value"] == "second"

    async def test_with_no_writer_it_writes_nothing(self) -> None:
        """Which is every deployment with no project token. The reader falls back to the App
        client, and the App holds no Projects permission of any kind, so there is nothing to
        fall back to for a write."""
        moved = await self.boards(None).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
        )

        assert moved.outcome is CardMove.NO_WRITER

    async def test_a_board_whose_status_field_cannot_be_read_writes_nothing(self) -> None:
        writer = FakeWriter()
        client = FakeJson(fields=[{"id": 39516, "name": "Title"}])

        moved = await HttpProjectBoards(client, writer=writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
        )

        assert moved.outcome is CardMove.UNREADABLE
        assert writer.sent == []

    async def test_a_board_with_no_column_meaning_that_status_writes_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not a failure worth telling whoever ran the command: their change landed on GitHub
        and in Discord, and the board simply has nowhere to put it."""
        writer = FakeWriter()
        client = FakeJson(fields=[a_board(options=[{"id": "opt-todo", "name": "Todo"}])])

        with caplog.at_level("WARNING", logger="shannon.github.projects"):
            moved = await HttpProjectBoards(client, writer=writer).move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
            )

        assert moved.outcome is CardMove.NO_COLUMN
        assert writer.sent == []
        assert "no column this bot reads as" in caplog.text
        assert "done, closed" in caplog.text, "it offered no column name to use"

    async def test_a_refusal_from_github_is_not_swallowed_here(self) -> None:
        """The caller decides what a failed write means. Here it only has to not pretend."""
        writer = FakeWriter(error=GitHubRefusedError("Could not resolve to a node"))

        with pytest.raises(GitHubRefusedError):
            await self.boards(writer).move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
            )


# The Status column of a real board, read off GitHub's own default project template:
# Backlog, Ready, In progress, In review, Done. Option ids are strings, and a name arrives as
# a {raw, html} pair rather than a bare string - both checked here rather than assumed.
DEFAULT_TEMPLATE = {
    "id": 353672864,
    "name": "Status",
    "data_type": "single_select",
    "options": [
        {"id": "f75ad846", "name": {"raw": "Backlog", "html": "Backlog"}, "color": "GREEN"},
        {"id": "e18bf179", "name": {"raw": "Ready", "html": "Ready"}, "color": "BLUE"},
        {
            "id": "47fc9ee4",
            "name": {"raw": "In progress", "html": "In progress"},
            "color": "YELLOW",
        },
        {"id": "aba860b9", "name": {"raw": "In review", "html": "In review"}, "color": "PURPLE"},
        {"id": "98236657", "name": {"raw": "Done", "html": "Done"}, "color": "ORANGE"},
    ],
}


class TestABoardFromGitHubsOwnTemplate:
    """The default template, which is what most boards actually look like.

    It carries BOTH `In progress` and `In review`, and this bot reads both as IN_REVIEW - which
    is right for reading a column back and not enough for writing one. Taken in board order,
    `/status In review` moved a card to `In progress`.
    """

    def moving(self, writer: FakeWriter) -> HttpProjectBoards:
        return HttpProjectBoards(
            FakeJson(fields=[{"id": 353672862, "name": "Title"}, DEFAULT_TEMPLATE]), writer=writer
        )

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (Status.BACKLOG, "f75ad846"),
            (Status.NOT_REVIEWED, "e18bf179"),
            (Status.IN_REVIEW, "aba860b9"),
            (Status.DONE, "98236657"),
        ],
    )
    async def test_each_status_lands_in_the_column_a_person_would_pick(
        self, status: Status, expected: str
    ) -> None:
        writer = FakeWriter()

        await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=status
        )

        assert writer.sent[0][2]["fields"][0]["value"] == expected

    async def test_in_review_does_not_land_in_in_progress(self) -> None:
        """The bug this ordering exists to prevent, named on its own so a change that brings it
        back cannot be read as a harmless reshuffle."""
        writer = FakeWriter()

        await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.IN_REVIEW
        )

        assert writer.sent[0][2]["fields"][0]["value"] != "47fc9ee4", "it picked In progress"

    async def test_a_status_the_template_has_no_column_for_writes_nothing(self) -> None:
        """READY_FOR_MERGE. The template has `Ready`, which means something else here - work
        that is ready to be picked up, not work that is ready to merge - so there is genuinely
        nowhere to put it and inventing somewhere would be worse than declining."""
        writer = FakeWriter()

        moved = await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.READY_FOR_MERGE
        )

        assert moved.outcome is CardMove.NO_COLUMN
        assert writer.sent == []

    async def test_the_status_field_id_is_the_one_the_write_addresses(self) -> None:
        writer = FakeWriter()

        await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
        )

        assert writer.sent[0][2]["fields"][0]["id"] == 353672864


PRIORITY_FIELD_ROW = {
    "id": 353672876,
    "name": "Priority",
    "data_type": "single_select",
    "options": [
        {"id": "79628723", "name": {"raw": "HIGH", "html": "HIGH"}, "color": "RED"},
        {"id": "0a877460", "name": {"raw": "MEDIUM", "html": "MEDIUM"}, "color": "ORANGE"},
        {"id": "da944a9c", "name": {"raw": "LOW", "html": "LOW"}, "color": "YELLOW"},
    ],
}


class TestSettingAPriority:
    """A board may carry a Priority single-select, and a real one does - option names matching
    this bot's own, in capitals. It is optional where Status is not: a board without one mirrors
    perfectly well and simply cannot be told a priority.
    """

    def moving(self, writer: FakeWriter, *, with_priority: bool = True) -> HttpProjectBoards:
        rows: list[Any] = [{"id": 353672862, "name": "Title"}, DEFAULT_TEMPLATE]
        if with_priority:
            rows.append(PRIORITY_FIELD_ROW)
        return HttpProjectBoards(FakeJson(fields=rows), writer=writer)

    @pytest.mark.parametrize(
        ("priority", "expected"),
        [
            (Priority.HIGH, "79628723"),
            (Priority.MEDIUM, "0a877460"),
            (Priority.LOW, "da944a9c"),
        ],
    )
    async def test_each_priority_lands_in_its_own_option(
        self, priority: Priority, expected: str
    ) -> None:
        """`HIGH` on the board and `High` here are the same word once both are normalised."""
        writer = FakeWriter()

        await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=priority
        )

        assert writer.sent[0][2]["fields"][0]["value"] == expected

    async def test_it_addresses_the_priority_field_not_the_status_one(self) -> None:
        """The callee picks the field from what it was given, which is why one method over the
        union is safer than two: a caller cannot send a priority to the Status field."""
        writer = FakeWriter()

        await self.moving(writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Priority.HIGH
        )

        assert writer.sent[0][2]["fields"][0]["id"] == 353672876

    async def test_a_synonym_is_read_the_way_a_label_would_be(self) -> None:
        """`urgent` means HIGH to this bot wherever it is written, so a board spelling its top
        column that way is understood - the same table that reads a priority off a label."""
        writer = FakeWriter()
        client = FakeJson(
            fields=[
                DEFAULT_TEMPLATE,
                {
                    "id": 353672876,
                    "name": "Priority",
                    "options": [{"id": "urgent-id", "name": "Urgent"}],
                },
            ]
        )

        await HttpProjectBoards(client, writer=writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Priority.HIGH
        )

        assert writer.sent[0][2]["fields"][0]["value"] == "urgent-id"

    async def test_a_board_with_no_priority_field_writes_nothing(self) -> None:
        """Not a misconfiguration. GitHub's default template ships no Priority field at all."""
        writer = FakeWriter()

        moved = await self.moving(writer, with_priority=False).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Priority.HIGH
        )

        assert moved.outcome is CardMove.NO_FIELD
        assert writer.sent == []

    async def test_a_priority_field_with_no_usable_options_is_no_field(self) -> None:
        """One shape for nothing to write to, rather than two. A select with no options is a
        field a write can address and never satisfy."""
        writer = FakeWriter()
        client = FakeJson(
            fields=[DEFAULT_TEMPLATE, {"id": 353672876, "name": "Priority", "options": []}]
        )

        moved = await HttpProjectBoards(client, writer=writer).move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Priority.HIGH
        )

        assert moved.outcome is CardMove.NO_FIELD

    async def test_the_status_field_still_works_on_the_same_board(self) -> None:
        """Both selects live in one cache entry, so reading one must not disturb the other."""
        writer = FakeWriter()
        boards = self.moving(writer)

        await boards.move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Priority.HIGH
        )
        await boards.move_card(
            owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
        )

        assert [sent[2]["fields"][0]["id"] for sent in writer.sent] == [353672876, 353672864]

    async def test_the_request_does_not_ask_for_the_priority_field(self) -> None:
        """`fields=` governs the READ, and nothing reads a card's priority back. Naming it there
        would send a field per card per poll that nothing parses."""
        client = FakeJson(
            fields=[{"id": 353672862, "name": "Title"}, DEFAULT_TEMPLATE, PRIORITY_FIELD_ROW],
            items=[],
        )

        await HttpProjectBoards(client).list_board_items("monalisa", PROJECT)

        asked = next(params for path, params in client.calls if path.endswith("/items"))
        assert asked["fields"] == "353672862,353672864"


class TestSayingItOnce:
    """Nothing retries a card write, so this cannot repeat on a timer - the ceiling is the rate
    people run the command. A team whose board has no merge-gate column would otherwise write a
    byte-identical line dozens of times a day."""

    def moving(self, writer: FakeWriter) -> HttpProjectBoards:
        return HttpProjectBoards(FakeJson(fields=[DEFAULT_TEMPLATE]), writer=writer)

    async def test_the_same_complaint_is_said_once(self, caplog: pytest.LogCaptureFixture) -> None:
        boards = self.moving(FakeWriter())

        with caplog.at_level("WARNING", logger="shannon.github.projects"):
            await boards.move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.READY_FOR_MERGE
            )
            await boards.move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.READY_FOR_MERGE
            )

        assert caplog.text.count("has no column this bot reads as") == 1

    async def test_a_different_state_is_a_different_complaint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Keyed on the state as well as the board. A board with no Ready for merge column almost
        certainly HAS a Done one, and keying on the board alone would swallow a real complaint
        about something else."""
        writer = FakeWriter()
        client = FakeJson(fields=[{"id": 39518, "name": "Status", "options": []}])
        boards = HttpProjectBoards(client, writer=writer)

        with caplog.at_level("WARNING", logger="shannon.github.projects"):
            await boards.move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.READY_FOR_MERGE
            )
            await boards.move_card(
                owner="monalisa", project_number=PROJECT, card_id=99, state=Status.DONE
            )

        assert caplog.text.count("has no column this bot reads as") == 2


class TestOpeningABoardThatRefuses:
    """A 404 and a 403 are one answer here: the board cannot be opened with what this has.

    Left raw they reach whoever ran the command as GitHub's own sentence, which names neither
    the board nor the credential - and the credential is the likeliest cause and the one with
    four separate ways of being wrong.
    """

    class Refusing:
        def __init__(self, error: Exception) -> None:
            self.error = error

        async def get_json(self, path: str, **params: Any) -> Any:
            if path.endswith(f"/projectsV2/{PROJECT}"):
                raise self.error
            return {"type": "User"}

        async def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]:
            yield []

    @pytest.mark.parametrize(
        "error",
        [
            GitHubAuthError("GitHub refused the request for /users/x/projectsV2/6 (403)"),
            GitHubNotFoundError("no such board"),
        ],
        ids=["refused", "not found"],
    )
    async def test_it_answers_no_board_rather_than_raising(self, error: Exception) -> None:
        found = await HttpProjectBoards(self.Refusing(error)).get_board("monalisa", PROJECT)

        assert found is None

    async def test_anything_else_still_raises(self) -> None:
        """Only the two that mean "you cannot open this" are folded. A rate limit is the whole
        process being asked to wait and must not read as a board that is not there."""
        boards = HttpProjectBoards(self.Refusing(GitHubRateLimitError("slow down")))

        with pytest.raises(GitHubRateLimitError):
            await boards.get_board("monalisa", PROJECT)
