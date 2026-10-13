"""Answering an interaction, whatever state it is already in.

Both functions here are guards more than they are work. `reply` picks the right of Discord's two
send paths depending on whether the interaction has been answered; `defer` does nothing if it has.
Neither guard is reached by any command, because each defers once and replies once, and both exist
so that a command doing otherwise fails visibly rather than by leaving somebody at a spinner.

Since issue #116 `reply` also picks between the two SHAPES a reply can take. A card carries no
content at all, and discord.py takes the absence of content as the sentinel that selects the
components overload, so each shape is its own call rather than a keyword on one.

And since issue #144 it pins that they are private. `EPHEMERAL` had been a constant nothing
asserted: the fakes took the keyword into `**_` and threw it away, so flipping it to False would
have published every command reply in this project and passed the entire suite.

Issue #147 took the string away. `reply` accepts a `Panel` and nothing else, so a reply carrying
no outcome mark is not a thing that can be written rather than a thing nobody happens to write,
and both type checkers hold it. A panel with no accent still goes out as content, which is the
one path a command takes when it reports rather than acts.

Issue #240 added an answer that goes away: `reply_then_delete` deletes it once its moment has
passed, which is how `/remind` keeps a countdown from running on past zero. Plain `reply` is
pinned never to delete anything, on every path, and a delete that fails is pinned to say
nothing.
"""

from __future__ import annotations

import gc
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import aiohttp
import discord
import pytest

from shannon.discord_bot import responses
from shannon.discord_bot.panels import Accent, Panel
from shannon.discord_bot.responses import (
    OWED,
    REFUSED,
    SUCCEEDED,
    defer,
    done,
    owed,
    refused,
    reply,
    reply_then_delete,
)
from shannon.discord_bot.safe_text import MESSAGE_LIMIT
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.conftest import Deletes


async def test_deferring_twice_does_not_answer_twice() -> None:
    """Discord rejects a second defer on one interaction, and the rejection is an exception in
    the command that would replace whatever it was about to say."""
    interaction = FakeInteraction()
    await defer(interaction)

    await defer(interaction)

    assert interaction.response.deferred is True
    assert interaction.followup.messages == []


async def test_deferring_something_already_answered_is_left_alone() -> None:
    interaction = FakeInteraction()
    await reply(interaction, Panel.of_text("done"))

    await defer(interaction)

    assert interaction.response.messages == ["done"]
    assert interaction.response.deferred is False


async def test_a_reply_after_a_defer_goes_through_the_followup() -> None:
    interaction = FakeInteraction()
    await defer(interaction)

    await reply(interaction, Panel.of_text("done"))

    assert interaction.followup.messages == ["done"]
    assert interaction.response.messages == []


async def test_an_over_long_reply_is_cut_rather_than_refused() -> None:
    """A slash command argument can be longer than a message may be, and several replies quote
    what was typed back. Refusing here would lose the refusal itself."""
    interaction = FakeInteraction()

    await reply(interaction, Panel.of_text("x" * (MESSAGE_LIMIT + 500)))

    sent = interaction.response.messages[0]
    assert len(sent) == MESSAGE_LIMIT, "a panel with no accent is cut against the panel budget"
    assert sent.endswith("…")


class TestAReplyThatIsACard:
    """Issue #116. A card carries no content at all, so it is a different send either way."""

    async def test_one_sent_first_goes_through_the_response(self) -> None:
        interaction = FakeInteraction()

        await reply(interaction, done("Registered acme/widget."))

        assert interaction.response.messages == [f"{SUCCEEDED} Registered acme/widget."]
        assert interaction.followup.messages == []

    async def test_one_sent_after_a_defer_goes_through_the_followup(self) -> None:
        interaction = FakeInteraction()
        await defer(interaction)

        await reply(interaction, done("Registered acme/widget."))

        assert interaction.followup.messages == [f"{SUCCEEDED} Registered acme/widget."]
        assert interaction.response.messages == []

    async def test_a_command_that_worked_is_green(self) -> None:
        assert done("Registered acme/widget.").accent == Accent.OPEN

    async def test_it_is_a_card_rather_than_a_string(self) -> None:
        """A panel with an accent is sent as components and one without is sent as content, so
        the distinction survives `reply` taking nothing but panels (#147)."""
        assert not done("Registered acme/widget.").is_plain
        assert Panel.of_text("Mentions are on.").is_plain

    async def test_an_over_long_card_is_cut_the_same_way_a_string_is(self) -> None:
        """The same argument, at the same place. A command quoting a long argument back can
        overflow whichever shape it answers in."""
        card = done("x" * (MESSAGE_LIMIT + 500))

        assert card.length() == MESSAGE_LIMIT
        assert card.text.endswith("…")


class TestNobodyElseSeesAReply:
    """`EPHEMERAL = True`, held against all four paths a reply can take.

    Thread traffic is the signal and an acknowledgement is not, which is the whole argument for
    the constant. Several replies also quote back what somebody typed, and five commands hand
    out one-time links. Since #201's review nobody but whoever ran the command could finish one,
    but a sign-in in progress is still nobody else's business.
    """

    async def test_an_immediate_reply_is_private(self) -> None:
        interaction = FakeInteraction()

        await reply(interaction, Panel.of_text("done"))

        assert interaction.ephemerally == [True]

    async def test_a_reply_after_a_defer_is_private_too(self) -> None:
        """The followup path, which is the one almost every command actually takes: they defer
        before talking to GitHub and answer afterwards."""
        interaction = FakeInteraction()
        await defer(interaction)

        await reply(interaction, Panel.of_text("done"))

        assert interaction.ephemerally == [True]

    async def test_a_card_is_private_on_both_paths(self) -> None:
        """A card goes out through a different discord.py overload from a string, so it is a
        different call with its own keyword to forget."""
        immediate = FakeInteraction()
        deferred = FakeInteraction()
        await defer(deferred)

        await reply(immediate, done("Registered acme/widget."))
        await reply(deferred, done("Registered acme/widget."))

        assert immediate.ephemerally == [True]
        assert deferred.ephemerally == [True]

    async def test_the_defer_itself_is_private(self) -> None:
        """Discord shows a public "thinking" message for a defer that is not, so this one leaks
        before the reply it precedes has been written."""
        interaction = FakeInteraction()

        await defer(interaction)

        assert interaction.response.deferred_ephemerally is True

    async def test_a_reply_that_goes_away_is_private_on_both_paths(self) -> None:
        """Issue #240 routes a countdown's answer through `reply_then_delete`, and gave the followup
        `wait=True`, which is another way into the same two calls in which to drop the keyword
        that matters."""
        immediate = FakeInteraction()
        deferred = FakeInteraction()
        await defer(deferred)

        await reply_then_delete(immediate, done("Set."), after=timedelta(minutes=5))
        await reply_then_delete(deferred, done("Set."), after=timedelta(minutes=5))

        assert immediate.ephemerally == [True]
        assert deferred.ephemerally == [True]


class TestAReplyThatGoesAway:
    """Issue #240. An answer deleted once its moment has passed, after a wait, in the background."""

    async def test_a_card_after_a_defer_is_deleted_after_the_wait(self, deletes: Deletes) -> None:
        """The path `/remind` takes: it defers before writing the reminder down."""
        interaction = FakeInteraction()
        await defer(interaction)

        await reply_then_delete(interaction, done("Set."), after=timedelta(minutes=5))
        await deletes.settle()

        assert interaction.followup.messages == [f"{SUCCEEDED} Set."]
        assert (deletes.waits, interaction.deleted) == ([300.0], [True])

    async def test_a_card_sent_first_is_deleted_after_the_wait(self, deletes: Deletes) -> None:
        """Through the first response, which is what it is."""
        interaction = FakeInteraction()

        await reply_then_delete(interaction, done("Set."), after=timedelta(minutes=5))
        await deletes.settle()

        assert interaction.response.messages == [f"{SUCCEEDED} Set."]
        assert (deletes.waits, interaction.deleted) == ([300.0], [True])

    @pytest.mark.parametrize("deferred", [False, True], ids=["first", "after a defer"])
    async def test_plain_text_is_deleted_after_the_wait_too(
        self, deletes: Deletes, deferred: bool
    ) -> None:
        interaction = FakeInteraction()
        if deferred:
            await defer(interaction)

        await reply_then_delete(interaction, Panel.of_text("Set."), after=timedelta(seconds=90))
        await deletes.settle()

        assert interaction.replies == ["Set."]
        assert (deletes.waits, interaction.deleted) == ([90.0], [True])

    async def test_nothing_goes_before_the_wait_is_over(self) -> None:
        """Without the stand-in, the delete is still asleep when the answer has been sent."""
        interaction = FakeInteraction()
        await defer(interaction)

        await reply_then_delete(interaction, done("Set."), after=timedelta(minutes=5))

        assert interaction.deleted == [False]

    @pytest.mark.parametrize("deferred", [False, True], ids=["first", "after a defer"])
    @pytest.mark.parametrize("message", [done("Set."), Panel.of_text("Set.")], ids=["card", "text"])
    async def test_an_ordinary_reply_is_never_deleted(
        self, deletes: Deletes, deferred: bool, message: Panel
    ) -> None:
        """Every answer but a countdown's stays, on every path a reply can take."""
        interaction = FakeInteraction()
        if deferred:
            await defer(interaction)

        await reply(interaction, message)
        await deletes.settle()

        assert (deletes.waits, interaction.deleted) == ([], [False])

    @pytest.mark.parametrize(
        "failure",
        [
            discord.NotFound(MagicMock(status=404), "Unknown Message"),
            aiohttp.ServerDisconnectedError(),
            RuntimeError("Session is closed"),
        ],
        ids=["dismissed first", "connection dropped", "shutting down"],
    )
    async def test_a_delete_that_fails_says_nothing(
        self, deletes: Deletes, failure: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Found reviewing #240. discord.py's own delayed delete let everything but an HTTP error
        out of its task, for asyncio to report at ERROR as an exception nobody retrieved. The
        answer only stays, as every answer did before, which is nobody's problem."""
        interaction = FakeInteraction()
        interaction.followup.delete_fails = failure
        await defer(interaction)

        with caplog.at_level(logging.ERROR, logger="asyncio"):
            await reply_then_delete(interaction, done("Set."), after=timedelta(minutes=5))
            await deletes.settle()
            gc.collect()

        assert interaction.deleted == [False]
        assert "never retrieved" not in caplog.text
        assert not responses._DELETES, "a finished delete was held on to"


class TestTheThreeOutcomesAReplyCanHave:
    """Issue #147. Every command reply is one of three things, and says which before it is read.

    Asserted here and nowhere else. The constructors are the only place a mark is put on, so a
    hundred command tests repeating these three characters would pin nothing the three below do
    not, and would have to be rewritten together if a mark ever changed.
    """

    def test_a_command_that_worked_is_ticked_and_green(self) -> None:
        card = done("Registered acme/widget.")

        assert card.text == f"{SUCCEEDED} Registered acme/widget."
        assert card.accent == Accent.OPEN

    def test_something_still_owed_is_warned_and_amber(self) -> None:
        """A rate limit that comes right on its own, a link nobody has followed yet, and a
        command whose second half did not land. None is a failure and none is finished."""
        card = owed("Open this link and sign in to GitHub.")

        assert card.text == f"{OWED} Open this link and sign in to GitHub."
        assert card.accent == Accent.MEDIUM

    def test_something_refused_is_crossed_and_red(self) -> None:
        card = refused("Run this inside a server channel.")

        assert card.text == f"{REFUSED} Run this inside a server channel."
        assert card.accent == Accent.FAILED

    def test_the_three_marks_are_different(self) -> None:
        """Two the same would make the bar the only thing telling them apart, which is what the
        marks were added to stop being true."""
        assert len({SUCCEEDED, OWED, REFUSED}) == 3
