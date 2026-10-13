"""`/remind`: ping somebody once, after the time you give. Issue #229.

Remembering things across several projects is the problem; a reminder that goes off in the channel
it was asked for, once, is the answer. Never again after that: recurring pings are not this.

Gated on one of its two halves, the way `/link` is and for `/link`'s reason. Reminding yourself
needs no role. Reminding somebody else has this bot ping them in public on your say-so, and a bot
that will ping anybody on anybody's say-so is a spam tool, so that half takes the tier that speaks
for the server.

A reminder goes off where it was asked for, perhaps a year later, with nobody there to tell if it
cannot. So everything that would stop it arriving is refused now, while there is somebody to tell:
a bot to remind, a person who cannot see the channel, a caller who could not post there themselves
- which is also what keeps this from posting somebody's words into a read-only channel - and a bot
that has not been given what posting there takes.

The private answer says when. A Discord countdown does not stop at zero, and left alone the answer
would go on to read "2 minutes ago" once the reminder had gone off. So a wait short enough is
counted down and the answer deleted as it reaches zero, and a longer one, which Discord no longer
lets this bot reach by then, gives the time and how long after setting it that is, and nothing in
it counts. Issue #240.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.db.models import REMINDER_MESSAGE_WIDTH
from shannon.discord_bot.formatting import as_relative_time, as_timestamp
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, refused, reply, reply_then_delete
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError

# The longest wait whose answer counts down and is deleted at zero. Issue #240. A bot may delete its
# answer to a command only while the command's token lives, fifteen minutes from when it was run,
# and the defer, the write and the send spend some of that before the countdown starts, so a minute
# is left for them. Waits are whole minutes: this is days and hours at nought and up to 14 minutes.
LONGEST_COUNTDOWN = timedelta(minutes=14)

A_BOT = "A bot cannot be reminded of anything. Pick a person."
NO_TIME = "Say when: at least one of days, hours and minutes has to be more than zero."
YOU_CANNOT_POST = (
    "You cannot post in this channel, so a reminder cannot go off here for you either. Set it "
    "somewhere you can post."
)


class SetsReminders(Protocol):
    """Writing a reminder down, answering when it falls due."""

    async def add(
        self,
        *,
        guild_id: int,
        channel_id: int,
        member_id: int,
        set_by: int,
        after: timedelta,
        message: str | None,
    ) -> datetime: ...


def build_remind_command(reminders: SetsReminders, gate: PermissionGate) -> SlashCommand:
    """Gated for one of its two halves, which keeps it out of `_permissions.UNGATED`.

    The tier on the other half is held by this command's own tests, as `/link`'s is.
    """

    @app_commands.command(
        name="remind", description="Ping somebody here once, after the time you give"
    )
    @app_commands.describe(
        member="Who to remind: you, or somebody else if your role allows it",
        days="Days from now, up to 365",
        hours="Hours on top of the days, up to 23",
        minutes="Minutes on top of those, up to 59",
        message="What to say with the ping, up to 500 characters",
    )
    @app_commands.guild_only()
    async def remind(
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Range[int, 0, 365] = 0,
        hours: app_commands.Range[int, 0, 23] = 0,
        minutes: app_commands.Range[int, 0, 59] = 0,
        message: app_commands.Range[str, None, REMINDER_MESSAGE_WIDTH] | None = None,
    ) -> None:
        # The guild check by hand rather than through `in_a_server`, which exists to apply a tier
        # and there is only a tier on one branch of this. `/link` does the same.
        guild_id, channel_id = interaction.guild_id, interaction.channel_id
        if guild_id is None or channel_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        # On identity rather than on who was typed, so naming yourself is a reminder for yourself.
        for_somebody_else = member.id != interaction.user.id
        if for_somebody_else and not gate.allows(interaction.user, REGISTER_ROLES):
            denial = gate.denial("remind", REGISTER_ROLES)
            await reply(interaction, refused(f"{denial} Reminding yourself needs no role."))
            return

        if member.bot:
            await reply(interaction, refused(A_BOT))
            return

        # What Discord resolved for the member named, in this channel. Absent rather than empty
        # where it said nothing, and an absence is not guessed at.
        seen = member.resolved_permissions
        if seen is not None and not seen.view_channel:
            await reply(
                interaction,
                refused(
                    f"A reminder here would never reach <@{member.id}>, who cannot see this "
                    "channel."
                ),
            )
            return

        after = timedelta(days=days, hours=hours, minutes=minutes)
        if after <= timedelta(0):
            await reply(interaction, refused(NO_TIME))
            return

        # Where it would go off, as Discord sent it with the command. Whether a thread is locked is
        # known now, and it decides what posting there will take when the reminder goes off.
        channel = interaction.channel
        in_a_thread = isinstance(channel, discord.Thread)
        locked = isinstance(channel, discord.Thread) and channel.locked
        if _cannot_post(interaction.permissions, in_a_thread=in_a_thread, locked=locked):
            await reply(interaction, refused(YOU_CANNOT_POST))
            return
        if missing := _cannot_post(
            interaction.app_permissions, in_a_thread=in_a_thread, locked=locked
        ):
            lacking = _listed(missing, last="or")
            await reply(
                interaction,
                refused(
                    "This bot cannot post in this channel, so the reminder would never go off. "
                    f"It has not been given {lacking} here."
                ),
            )
            return

        await defer(interaction)
        try:
            due = await reminders.add(
                guild_id=guild_id,
                channel_id=channel_id,
                member_id=member.id,
                set_by=interaction.user.id,
                after=after,
                message=message,
            )
        except ShannonError as error:
            # The ceiling on waiting reminders, which comes right on its own as they go off.
            await reply(interaction, reply_for(error))
            return

        who = f"<@{member.id}>" if for_somebody_else else "you"
        if after <= LONGEST_COUNTDOWN:
            # Counted down live, and deleted as it reaches zero, while Discord still lets this bot
            # reach the answer then.
            countdown = f"{as_relative_time(due)}, on {as_timestamp(due)}"
            await reply_then_delete(
                interaction, done(f"This bot will remind {who} here {countdown}."), after=after
            )
            return
        # Further off, nothing could stop a countdown at zero, so there is none to stop.
        took = _how_long(days=days, hours=hours, minutes=minutes)
        await reply(
            interaction,
            done(
                f"This bot will remind {who} here on {as_timestamp(due)}, {took} after you set it."
            ),
        )

    # `app_commands.command()` leaves the command's binding type unknown; one line here rather
    # than a suppression over the whole file.
    return remind  # pyright: ignore[reportUnknownVariableType]


def _cannot_post(permissions: discord.Permissions, *, in_a_thread: bool, locked: bool) -> list[str]:
    """The permissions somebody lacks to post here, named as Discord's settings name them.

    In a thread, posting is Send Messages in Threads rather than Send Messages: Discord keeps the
    two apart, and a channel can allow either without the other. A locked thread takes Manage
    Threads as well, both to post in and to reopen. It needs reopening by the time a reminder goes
    off, because nobody can post in it to keep it from archiving, and without the permission a
    reminder that was accepted would be refused then, with nobody left to tell.
    """
    wanted = [
        ("View Channel", permissions.view_channel),
        (
            ("Send Messages in Threads", permissions.send_messages_in_threads)
            if in_a_thread
            else ("Send Messages", permissions.send_messages)
        ),
    ]
    if locked:
        wanted.append(("Manage Threads", permissions.manage_threads))
    return [name for name, held in wanted if not held]


def _how_long(*, days: int, hours: int, minutes: int) -> str:
    """The wait as it was typed: `1 day`, `2 hours and 30 minutes`, `2 days, 3 hours and 1 minute`.

    A count of nought is left out rather than read back as "0 hours". All three at nought is a wait
    of nothing, which was refused before anything was written down. Issue #240.
    """
    counted = [
        f"{count} {unit}" if count == 1 else f"{count} {unit}s"
        for count, unit in ((days, "day"), (hours, "hour"), (minutes, "minute"))
        if count
    ]
    return _listed(counted, last="and")


def _listed(names: list[str], *, last: str) -> str:
    """`A`, `A or B`, `A, B or C`, joined with `last`: a list read as a sentence."""
    if len(names) < 3:
        return f" {last} ".join(names)
    return f"{', '.join(names[:-1])} {last} {names[-1]}"
