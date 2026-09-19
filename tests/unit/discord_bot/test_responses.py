"""Answering an interaction, whatever state it is already in.

Both functions here are guards more than they are work. `reply` picks the right of Discord's two
send paths depending on whether the interaction has been answered; `defer` does nothing if it has.
Neither guard is reached by any command, because each defers once and replies once, and both exist
so that a command doing otherwise fails visibly rather than by leaving somebody at a spinner.

Since issue #116 `reply` also picks between the two SHAPES a reply can take. A card carries no
content at all, and discord.py takes the absence of content as the sentinel that selects the
components overload, so each shape is its own call rather than a keyword on one.
"""

from __future__ import annotations

from shannon.discord_bot.panels import Accent
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.safe_text import MESSAGE_LIMIT
from tests.fakes.discord_objects import FakeInteraction


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
    await reply(interaction, "done")

    await defer(interaction)

    assert interaction.response.messages == ["done"]
    assert interaction.response.deferred is False


async def test_a_reply_after_a_defer_goes_through_the_followup() -> None:
    interaction = FakeInteraction()
    await defer(interaction)

    await reply(interaction, "done")

    assert interaction.followup.messages == ["done"]
    assert interaction.response.messages == []


async def test_an_over_long_reply_is_cut_rather_than_refused() -> None:
    """A slash command argument can be longer than a message may be, and several replies quote
    what was typed back. Refusing here would lose the refusal itself."""
    interaction = FakeInteraction()

    await reply(interaction, "x" * (MESSAGE_LIMIT + 500))

    sent = interaction.response.messages[0]
    assert len(sent) == MESSAGE_LIMIT
    assert sent.endswith("…")


class TestAReplyThatIsACard:
    """Issue #116. A card carries no content at all, so it is a different send either way."""

    async def test_one_sent_first_goes_through_the_response(self) -> None:
        interaction = FakeInteraction()

        await reply(interaction, done("Registered acme/widget."))

        assert interaction.response.messages == ["Registered acme/widget."]
        assert interaction.followup.messages == []

    async def test_one_sent_after_a_defer_goes_through_the_followup(self) -> None:
        interaction = FakeInteraction()
        await defer(interaction)

        await reply(interaction, done("Registered acme/widget."))

        assert interaction.followup.messages == ["Registered acme/widget."]
        assert interaction.response.messages == []

    async def test_a_command_that_worked_is_green(self) -> None:
        assert done("Registered acme/widget.").accent == Accent.OPEN

    async def test_it_is_a_card_rather_than_a_string(self) -> None:
        """A plain panel would be sent as content, which is the whole point of the distinction:
        the one-sentence replies stay strings and these do not."""
        assert not done("Registered acme/widget.").is_plain

    async def test_an_over_long_card_is_cut_the_same_way_a_string_is(self) -> None:
        """The same argument, at the same place. A command quoting a long argument back can
        overflow whichever shape it answers in."""
        card = done("x" * (MESSAGE_LIMIT + 500))

        assert card.length() == MESSAGE_LIMIT
        assert card.text.endswith("…")
