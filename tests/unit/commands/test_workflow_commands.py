"""`/status` and `/priority`, as Discord drives them.

They act on the thread they are run in, so the interaction's channel and the picked value are
the whole input. What is worth pinning is which of them exist, what their pickers offer, who
may run them, and that a refusal from the service comes back as a sentence rather than as
silence after a deferred interaction.

It was eight commands taking no argument. The two tables they were built from are now two
choice lists, and the reason each is written out rather than comprehended over its enum is
what the last two tests here hold.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.workflow import (
    MOST_CHOICES,
    OWN_NAMES,
    PRIORITY_CHOICES,
    build_workflow_commands,
)
from shannon.domain.board import status_from_column
from shannon.domain.enums import Priority, Status, spoken
from shannon.github import people
from shannon.github.labels import PRIORITY_LABELS
from shannon.services.workflow import NotAnItemThreadError, WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
    FakeAccess,
    administrator,
    default_gate,
    developer,
    member_with,
    project_manager,
)

THREAD_ID = 555


class StubWorkflow:
    def __init__(self, *, outcome: WorkflowOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome or WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=True)
        self.error = error
        self.calls: list[tuple[str, int, object]] = []
        # What the command passed down as the board column, which is the text somebody picked
        # rather than the status it read back as.
        self.columns: list[str] = []

    async def set_status(
        self, *, thread_id: int, status: Status, column: str = ""
    ) -> WorkflowOutcome:
        self.calls.append(("status", thread_id, status))
        self.columns.append(column)
        if self.error is not None:
            raise self.error
        return self.outcome

    async def set_priority(self, *, thread_id: int, priority: Priority) -> WorkflowOutcome:
        self.calls.append(("priority", thread_id, priority))
        if self.error is not None:
            raise self.error
        return self.outcome


class FakeColumns:
    """The board columns a server's picker offers, out of a tuple."""

    def __init__(self, *columns: str, error: Exception | None = None) -> None:
        self.columns = columns
        self.error = error
        self.asked: list[int] = []

    async def offered(self, guild_id: int) -> tuple[str, ...]:
        self.asked.append(guild_id)
        if self.error is not None:
            raise self.error
        return self.columns


def commands(
    service: StubWorkflow,
    access: FakeAccess | None = None,
    columns: FakeColumns | None = None,
) -> dict[str, app_commands.Command]:
    built = build_workflow_commands(
        service, default_gate(), access or FakeAccess(), columns or FakeColumns()
    )
    return {command.name: command for command in built}


async def run(
    name: str,
    service: StubWorkflow,
    member: FakeMember,
    value: str,
    access: FakeAccess | None = None,
) -> FakeInteraction:
    interaction = FakeInteraction(user=member, channel_id=THREAD_ID)
    # A plain string for status and a choice for priority, because that is the difference
    # between them now: a priority is the same three everywhere and stays a validated
    # dropdown, and a status is whatever columns somebody arranged on their board.
    handed: object = value if name == "status" else app_commands.Choice(name=value, value=value)
    await commands(service, access)[name].callback(interaction, handed)
    return interaction


def said(interaction: FakeInteraction) -> str:
    """The sentence, without the outcome mark `FakeInteraction.said` strips."""
    return interaction.said


def test_both_commands_are_built() -> None:
    """Two now, where there were eight. A missing one does not fail a test at boot: it
    stops existing in Discord."""
    assert sorted(commands(StubWorkflow())) == ["priority", "status"]


@pytest.mark.parametrize("status", list(Status))
async def test_each_status_can_be_picked(status: Status) -> None:
    service = StubWorkflow()

    await run("status", service, member_with("Project Manager"), status.value)

    assert service.calls == [("status", THREAD_ID, status)]


@pytest.mark.parametrize("priority", [p for p in Priority if p is not Priority.UNSET])
async def test_each_priority_can_be_picked(priority: Priority) -> None:
    service = StubWorkflow()

    await run("priority", service, member_with("Project Manager"), priority.value)

    assert service.calls == [("priority", THREAD_ID, priority)]


@pytest.mark.parametrize("who", [project_manager, administrator])
async def test_project_managers_and_administrators_may_run_them(who) -> None:
    service = StubWorkflow()

    await run("status", service, who(), spoken(Status.IN_REVIEW))

    assert service.calls, "somebody the requirements allow was refused"


async def test_a_developer_may_not() -> None:
    """The permissions table grants these to project managers. A developer marking their own
    work done is the review step going missing."""
    service = StubWorkflow()

    interaction = await run("status", service, developer(), spoken(Status.DONE))

    assert service.calls == []
    assert "You need one of these roles" in said(interaction)


async def test_the_reply_names_the_item_and_what_it_became() -> None:
    service = StubWorkflow()

    interaction = await run(
        "status", service, member_with("Project Manager"), spoken(Status.IN_REVIEW)
    )

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now In review."


async def test_a_repeat_says_so_rather_than_claiming_a_change() -> None:
    service = StubWorkflow(outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=False))

    interaction = await run(
        "status", service, member_with("Project Manager"), spoken(Status.BACKLOG)
    )

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is already Backlog."


async def test_finishing_says_the_thread_is_locked() -> None:
    service = StubWorkflow(
        outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=True, locked=True)
    )

    interaction = await run("status", service, member_with("Project Manager"), spoken(Status.DONE))

    assert said(interaction).endswith("is now Done, and this thread is locked.")


async def test_a_lock_discord_refused_says_what_did_happen_as_well() -> None:
    """Everything but the lock landed, and reporting only the refusal reads as the opposite.

    Somebody told the command failed goes and runs it again from the top, or worse, decides the
    item is not done and chases it. What it needs to say is that the item moved, that the one
    step left is a permission, and that running it again is what takes it.
    """
    service = StubWorkflow(
        outcome=WorkflowOutcome(
            "Canon-Regularis/Shannon-bot",
            7,
            changed=True,
            locked=False,
            lock_refused=True,
            wanted_locked=True,
        )
    )

    interaction = await run("status", service, member_with("Project Manager"), spoken(Status.DONE))

    answer = said(interaction)
    assert answer.startswith("Canon-Regularis/Shannon-bot#7 is Done")
    assert "could not be locked" in answer
    assert "Manage Threads" in answer


async def test_a_refused_unlock_says_nobody_can_reply_rather_than_the_opposite() -> None:
    """The two directions are not the same news.

    A thread that would not lock is untidy. A thread that would not unlock is shut against the
    discussion the person running this has just reopened it for, and they need to know that
    before they walk away from it.
    """
    service = StubWorkflow(
        outcome=WorkflowOutcome(
            "Canon-Regularis/Shannon-bot",
            7,
            changed=True,
            locked=True,
            lock_refused=True,
            wanted_locked=False,
        )
    )

    interaction = await run(
        "status", service, member_with("Project Manager"), spoken(Status.IN_REVIEW)
    )

    answer = said(interaction)
    assert "could not be unlocked" in answer
    assert "nobody can reply in it" in answer


async def test_a_refusal_comes_back_as_a_sentence() -> None:
    """The interaction has been deferred by this point, so anything escaping leaves the person
    who ran it watching a spinner until Discord gives up."""
    service = StubWorkflow(error=NotAnItemThreadError("Run this inside the item's thread."))

    interaction = await run(
        "status", service, member_with("Project Manager"), spoken(Status.IN_REVIEW)
    )

    assert said(interaction) == "Run this inside the item's thread."


async def test_a_priority_reply_reads_as_a_priority() -> None:
    service = StubWorkflow()

    interaction = await run(
        "priority", service, member_with("Project Manager"), Priority.HIGH.value
    )

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now High priority."


async def test_it_refuses_outside_a_server() -> None:
    """Guild-only is declared to Discord, and checked again because a declaration is not a gate."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), guild_id=None)

    await commands(service)["status"].callback(interaction, spoken(Status.IN_REVIEW))

    assert service.calls == []
    assert said(interaction) == "Run this inside a server channel."


async def test_it_refuses_with_no_channel_to_act_on() -> None:
    """The thread is the whole input, so an interaction without one has nothing to work from."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=None)

    await commands(service)["status"].callback(interaction, spoken(Status.IN_REVIEW))

    assert service.calls == []
    assert said(interaction) == "Run this inside the item's thread."


def test_the_status_picker_opens_on_the_state_work_starts_in() -> None:
    """Discord shows choices in the order they are written, and the enum is not in that
    order: it declares BACKLOG third, which is right for the database and wrong for a
    person. Comprehended over `Status`, the picker would open on Not reviewed with Backlog
    buried under In review.

    Written out rather than compared against `list(Status)` in some order, because the point is
    that this list and that enum disagree on purpose. It also happens to run in the order a
    board's columns usually do, which is worth nothing: the rule that refuses a skipped column
    reads the BOARD's order and never this one."""
    assert list(OWN_NAMES) == [
        spoken(Status.BACKLOG),
        spoken(Status.NOT_REVIEWED),
        spoken(Status.IN_REVIEW),
        spoken(Status.DONE),
    ]


def test_no_priority_can_be_picked_that_has_no_label_to_write() -> None:
    """The one test standing between somebody tidying the priority table into a
    comprehension and a KeyError in front of a user.

    `Priority` has four members and `PRIORITY_LABELS` has three: UNSET is what an item has
    before anybody says, not something to pick. `priority_change` ends in a lookup against
    that table, so a picked None would raise rather than refuse, and reach whoever ran it as
    "Something went wrong here". Eight commands could not express UNSET and so were immune
    to this; two commands with a picker are not."""
    assert {Priority(choice.value) for choice in PRIORITY_CHOICES} == set(PRIORITY_LABELS)


def test_between_them_the_pickers_offer_every_state_but_the_empty_one() -> None:
    """Re-homed from the domain tests, where it held the same invariant against a table
    of command names that no longer exists. Here rather than there because that file is on
    neither type-suppression list, and putting discord.py's choice types into it would be a
    new strict-typing surface in a file that has none.

    Through `status_from_column` for the status half, which is what the callback does with what
    the picker hands back. A name this picker offers that reads back as None would be an entry
    its own command refuses, and that is the failure this catches."""
    read_back = {status_from_column(name) for name in OWN_NAMES}
    offered: set[Status | Priority | None] = read_back | {
        Priority(c.value) for c in PRIORITY_CHOICES
    }

    assert offered | {Priority.UNSET} == {*Status, *Priority}


class TestWhatGitHubSays:
    """The second gate, added by issue #158. The Discord role says who may ask; this says whether
    the account they proved may actually write to the repository."""

    async def test_a_refusal_from_github_stops_the_command(self) -> None:
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub does not have monalisa as a collaborator.")
        interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=THREAD_ID)

        await commands(service, access)["status"].callback(interaction, spoken(Status.IN_REVIEW))

        assert service.calls == [], "it wrote to GitHub after GitHub said no"
        assert "not have monalisa as a collaborator" in said(interaction)

    async def test_the_role_is_checked_first(self) -> None:
        """Somebody without the Discord role is told that, rather than told about a GitHub
        account they may never have connected."""
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub says no.")
        interaction = FakeInteraction(user=developer(), channel_id=THREAD_ID)

        await commands(service, access)["status"].callback(interaction, spoken(Status.IN_REVIEW))

        assert "You need one of these roles" in said(interaction)
        assert access.asked == [], "it asked GitHub about somebody the role already refused"

    async def test_it_asks_for_write_rather_than_admin(self) -> None:
        """A collaborator with write is exactly who these commands are for. Asking for admin
        would refuse the reviewers the permission table grants them to."""
        service = StubWorkflow()
        access = FakeAccess()

        await run(
            "status", service, member_with("Project Manager"), spoken(Status.IN_REVIEW), access
        )

        assert [asked[2] for asked in access.asked] == [people.WRITE]

    async def test_priority_is_gated_too(self) -> None:
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub says no.")
        interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=THREAD_ID)

        await commands(service, access)["priority"].callback(
            interaction, app_commands.Choice(name="High", value=Priority.HIGH.value)
        )

        assert service.calls == []


class TestWhenTheBoardHadNowhereToPutIt:
    """Said as a caveat on a success rather than as a failure, which is what it is: the labels
    are on GitHub, the row is written and the thread is redrawn.

    The same shape the refused lock takes, and for the same reason - reporting only the half
    that did not land reads as nothing having happened.
    """

    def nowhere(self, **extra: object) -> WorkflowOutcome:
        fields: dict[str, object] = {"changed": True, "board_has_no_column": True}
        fields.update(extra)
        return WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, **fields)  # type: ignore[arg-type]

    async def test_it_says_the_card_was_left_where_it_was(self) -> None:
        service = StubWorkflow(outcome=self.nowhere())

        interaction = await run(
            "status", service, member_with("Project Manager"), spoken(Status.BACKLOG)
        )

        answer = said(interaction)
        assert "is now Backlog" in answer
        assert "no column called Backlog" in answer

    async def test_it_says_what_to_do_about_it(self) -> None:
        service = StubWorkflow(outcome=self.nowhere())

        interaction = await run(
            "status", service, member_with("Project Manager"), spoken(Status.DONE)
        )

        assert "rename a column to match, or move it by hand" in said(interaction)

    async def test_it_does_not_claim_which_column_the_card_is_in(self) -> None:
        """Nothing read the card. Saying where it is would be a guess in the one message whose
        whole job is to be trustworthy about the board."""
        service = StubWorkflow(outcome=self.nowhere())

        interaction = await run(
            "status", service, member_with("Project Manager"), spoken(Status.DONE)
        )

        assert "is in" not in said(interaction)

    async def test_a_repeat_says_it_too(self) -> None:
        """The repeat is where somebody lands after the first one did nothing visible, so it is
        the worst possible place to go quiet about it."""
        service = StubWorkflow(outcome=self.nowhere(changed=False))

        interaction = await run(
            "status", service, member_with("Project Manager"), spoken(Status.DONE)
        )

        answer = said(interaction)
        assert "is already Done" in answer
        assert "no column called Done" in answer

    async def test_an_ordinary_move_says_nothing_extra(self) -> None:
        service = StubWorkflow()

        interaction = await run(
            "status", service, member_with("Project Manager"), spoken(Status.IN_REVIEW)
        )

        assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now In review."

    async def test_priority_says_it_too(self) -> None:
        service = StubWorkflow(outcome=self.nowhere())

        interaction = await run(
            "priority", service, member_with("Project Manager"), Priority.HIGH.value
        )

        assert "no column called High" in said(interaction)


class TestThePicker:
    """The board's own columns, then this bot's own names, and nothing it would then refuse.

    A status used to be a closed list of four baked into the registration at `tree.sync()`. That is
    once, at boot, globally, so a list that differs per server has to be a picker - and a board is
    exactly a thing that differs per server. What it costs falls due here: three seconds to answer,
    nowhere to put a refusal, and a callback that has to parse what comes back.
    """

    async def suggest(self, columns: FakeColumns, typed: str = "") -> list[str]:
        picker = commands(StubWorkflow(), columns=columns)["status"]
        found = await picker._params["to"].autocomplete(  # pyright: ignore[reportPrivateUsage]
            FakeInteraction(user=project_manager(), channel_id=THREAD_ID), typed
        )
        return [choice.name for choice in found]

    async def test_the_boards_own_columns_come_first(self) -> None:
        """They are what somebody is looking at. A board carrying `In progress` and `In review` has
        two columns this bot reads as one status, and choosing between them is the whole reason this
        is a picker rather than a list of four."""
        offered = await self.suggest(FakeColumns("Backlog", "Ready", "In progress", "In review"))

        assert offered[:4] == ["Backlog", "Ready", "In progress", "In review"]

    async def test_this_bots_own_names_fill_the_gaps_and_nothing_else(self) -> None:
        """Behind the board's columns, and only for a status the board has no column for at all.

        A board calling it `Todo` where this bot says `Not reviewed` means the same thing, and
        offering both is two entries doing one job with only one of them written on the board
        somebody is looking at. What stays is `Done`, which that board genuinely has nowhere for -
        so picking it gets the sentence saying so rather than silence.
        """
        offered = await self.suggest(FakeColumns("Todo", "Doing"))

        assert offered[:2] == ["Todo", "Doing"]
        assert "Not reviewed" not in offered, "two entries for one status"
        assert "In review" not in offered, "`Doing` already covers it"
        assert "Backlog" in offered
        assert "Done" in offered

    async def test_a_column_this_bot_cannot_read_is_not_offered(self) -> None:
        """Offering it would be offering an entry its own callback then refuses.
        `status_from_column` is the same function the callback uses, so the two cannot disagree
        about what is pickable."""
        offered = await self.suggest(FakeColumns("Backlog", "Needs design input"))

        assert "Backlog" in offered
        assert "Needs design input" not in offered

    async def test_a_board_spelling_one_of_our_names_is_offered_once(self) -> None:
        """`In review` from the board and `In review` from this bot are the same column. Two entries
        doing one thing is a picker that looks broken."""
        offered = await self.suggest(FakeColumns("In review"))

        assert offered.count("In review") == 1

    async def test_a_server_with_no_board_still_has_a_picker(self) -> None:
        offered = await self.suggest(FakeColumns())

        assert offered == list(OWN_NAMES)

    async def test_typing_narrows_it(self) -> None:
        offered = await self.suggest(FakeColumns("Backlog", "In progress", "In review"), "in")

        assert offered == ["In progress", "In review"]

    async def test_a_column_too_long_for_discord_is_left_out(self) -> None:
        """Discord rejects a choice whose value runs past a hundred characters, and rejects the
        whole list rather than the one entry - so one absurd column name would take the picker
        down for every other. The column this bot stores is a hundred and twenty-eight wide, so
        this is reachable rather than theoretical.

        Padded with spaces rather than with punctuation, so the name stays one this bot READS as
        a status - `normalise` collapses the run away. A name full of exclamation marks would be
        dropped for being unreadable and would prove nothing about the length."""
        offered = await self.suggest(FakeColumns("Backlog", "Done" + " " * 120))

        assert "Backlog" in offered
        assert not any(len(name) > 100 for name in offered)

    async def test_it_stays_inside_discords_cap(self) -> None:
        """Discord sends a longer list back as an error rather than truncating it, so going
        over takes the whole picker down.

        Contrived on purpose, and worth saying so: a board cannot realistically carry twenty-five
        columns this bot reads as a status, because `_COLUMNS` has fewer names than that in it.
        The spellings below are the same few columns cased differently, which is enough to reach
        the slice. This is the guard being exercised rather than a board anybody has."""
        spellings = [
            f"{name}{' ' * pad}"
            for pad in range(9)
            for name in ("Backlog", "Todo", "In progress", "Done")
        ]
        offered = await self.suggest(FakeColumns(*spellings))

        assert len(spellings) > MOST_CHOICES, "the fixture cannot reach the cap"
        assert len(offered) == MOST_CHOICES

    async def test_a_board_that_will_not_read_leaves_the_picker_working(self) -> None:
        """An autocomplete that raises shows the person nothing at all, so a GitHub outage would be
        indistinguishable from a board with no columns - and there is nowhere here to put a
        refusal."""
        offered = await self.suggest(FakeColumns(error=RuntimeError("GitHub is down")))

        assert offered == list(OWN_NAMES)

    async def test_outside_a_server_it_offers_this_bots_own_names(self) -> None:
        columns = FakeColumns("Backlog")
        picker = commands(StubWorkflow(), columns=columns)["status"]

        found = await picker._params["to"].autocomplete(  # pyright: ignore[reportPrivateUsage]
            FakeInteraction(user=project_manager(), channel_id=THREAD_ID, guild_id=None), ""
        )

        assert [choice.name for choice in found] == list(OWN_NAMES)
        assert columns.asked == [], "it looked up a board with no server to look one up for"


class TestWhatArrivesInTheStatusField:
    """A suggestion is only a suggestion. discord.py resolved a closed choice list before the
    callback ran; an autocomplete does not, so what arrives may have been typed, may be a column
    from the board this repository mirrored last week, or may be prose."""

    async def test_a_board_column_is_read_as_the_status_it_stands_for(self) -> None:
        service = StubWorkflow()

        await run("status", service, project_manager(), "In progress")

        assert service.calls == [("status", THREAD_ID, Status.IN_REVIEW)]

    async def test_the_column_goes_down_beside_the_status(self) -> None:
        """The status decides the label, the lock and the block in Discord. The column decides which
        of the board's own columns the card lands in, and on a board with two columns for one status
        those are not the same question."""
        service = StubWorkflow()

        await run("status", service, project_manager(), "In progress")

        assert service.columns == ["In progress"]

    @pytest.mark.parametrize("typed", ["Needs design input", "", "   ", "nonsense"])
    async def test_something_that_is_not_a_status_is_refused(self, typed: str) -> None:
        service = StubWorkflow()

        interaction = await run("status", service, project_manager(), typed)

        assert "is not a status this bot knows" in said(interaction)
        assert service.calls == []

    async def test_the_refusal_names_what_always_works(self) -> None:
        """Somebody whose picker came back empty has otherwise been handed a text box and no
        vocabulary."""
        interaction = await run("status", StubWorkflow(), project_manager(), "nonsense")

        for name in OWN_NAMES:
            assert name in said(interaction)
