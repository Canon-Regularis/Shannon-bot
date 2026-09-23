"""`/mentions`, the one command anybody may run. Issue #80.

Worth asserting the exact wording rather than a clause of it, for the same reason `/refresh`
does: the reply is the whole of what somebody gets back, it is ephemeral so nobody else can
correct it, and a sentence that is subtly wrong about whether they will be notified is worse
than no sentence at all.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.mentions import build_mentions_command
from tests.fakes.discord_objects import FakeGuildPermissions, FakeInteraction, FakeMember

pytestmark = pytest.mark.unit


class StubPreferences:
    """Records what it was asked, and answers whatever it was told to."""

    def __init__(self, *, wanted: bool = True) -> None:
        self.wanted = wanted
        self.asked: list[dict] = []
        self.set: list[dict] = []

    async def wants_mentions(self, *, guild_id: int, discord_user_id: int) -> bool:
        self.asked.append({"guild_id": guild_id, "discord_user_id": discord_user_id})
        return self.wanted

    async def set_mentions(self, *, guild_id: int, discord_user_id: int, wanted: bool) -> None:
        self.set.append(
            {"guild_id": guild_id, "discord_user_id": discord_user_id, "wanted": wanted}
        )
        self.wanted = wanted


async def run(service: StubPreferences, state: str | None = None, **kwargs) -> FakeInteraction:
    interaction = FakeInteraction(**kwargs)
    choice = None if state is None else app_commands.Choice(name=state, value=state)
    await build_mentions_command(service).callback(interaction, choice)
    return interaction


class TestTurningThemOff:
    async def test_it_records_the_choice_against_the_caller(self) -> None:
        service = StubPreferences()
        member = FakeMember(id=4242)

        await run(service, "off", user=member)

        assert service.set == [{"guild_id": 1, "discord_user_id": 4242, "wanted": False}]

    async def test_the_reply_says_they_are_still_named(self) -> None:
        """The thing somebody is most likely to worry about before running it: whether going
        quiet also means disappearing off the items they are on."""
        interaction = await run(StubPreferences(), "off")

        assert "still be named" in interaction.reply

    async def test_the_reply_says_a_role_ping_still_reaches_them(self) -> None:
        """The one thing this command cannot do, and finding it out from a notification later is
        worse than being told now."""
        interaction = await run(StubPreferences(), "off")

        assert "Discord role" in interaction.reply


class TestTurningThemOn:
    async def test_it_records_the_choice(self) -> None:
        service = StubPreferences(wanted=False)

        await run(service, "on")

        assert service.set == [{"guild_id": 1, "discord_user_id": 1, "wanted": True}]

    async def test_the_reply_is_short_about_it(self) -> None:
        """No warning about roles here. It only matters to somebody who asked for quiet."""
        interaction = await run(StubPreferences(wanted=False), "on")

        assert (
            interaction.said == "Mentions are on. This bot will notify you about items you are on."
        )


class TestAskingWithoutSetting:
    async def test_no_argument_reports_on_without_changing_anything(self) -> None:
        service = StubPreferences(wanted=True)

        interaction = await run(service)

        assert service.set == [], "asking changed it"
        assert interaction.said.startswith("Mentions are on.")

    async def test_no_argument_reports_off(self) -> None:
        service = StubPreferences(wanted=False)

        interaction = await run(service)

        assert service.set == []
        assert interaction.said.startswith("Mentions are off.")
        assert "Discord role" in interaction.reply


class TestWhoMayRunIt:
    async def test_anybody_can_run_it(self) -> None:
        """The deliberate break: this is the only command in the bot with no role behind it.

        Every other one decides something about the server. This decides whether your own name
        notifies you, and gating it would mean asking a project manager to turn off your own
        pings. `test_permission_tables` is what holds that to `_permissions.UNGATED` rather than
        to a comment; this is the behaviour from the member's side.

        The mirror image of `test_link_command.py`'s
        `test_claiming_a_login_for_yourself_is_no_longer_ungated`, which records the opposite
        decision being taken for a command where it was the wrong one.
        """
        service = StubPreferences()
        nobody = FakeMember(id=7, roles=[], guild_permissions=FakeGuildPermissions())

        interaction = await run(service, "off", user=nobody)

        assert service.set == [{"guild_id": 1, "discord_user_id": 7, "wanted": False}]
        assert "not allowed" not in interaction.reply

    async def test_it_acts_on_the_caller_and_nobody_else(self) -> None:
        """There is deliberately no `member` argument. `/link` has one because pointing a login
        at an account is a decision about the server; silencing somebody else's pings is not a
        decision anybody should be able to make for them."""
        assert "member" not in build_mentions_command(StubPreferences())._params

    async def test_running_outside_a_server_is_refused(self) -> None:
        service = StubPreferences()

        interaction = await run(service, "off", guild_id=None)

        assert interaction.said == "Run this inside a server channel."
        assert service.set == []
