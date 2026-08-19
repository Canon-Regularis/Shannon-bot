"""Answering an interaction, whatever state it is already in.

Both functions here are guards more than they are work. `reply` picks the right of Discord's two
send paths depending on whether the interaction has been answered; `defer` does nothing if it has.
Neither guard is reached by any command, because each defers once and replies once, and both exist
so that a command doing otherwise fails visibly rather than by leaving somebody at a spinner.
"""

from __future__ import annotations

from shannon.discord_bot.responses import defer, reply
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
