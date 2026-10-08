"""A reminder, as the line it goes off with. Issue #229.

What is worth pinning is who it names and who it can ring. The person it is for is a mention at
the front of the block under the heading, the one place a panel over budget never cuts; whoever
asked for it is named beside them; and nothing in the message somebody typed comes out able to
ring anybody at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shannon.discord_bot.formatting import as_relative_time, format_reminder
from shannon.discord_bot.panels import PANEL_BUDGET, Accent, BlockKind, Panel

pytestmark = pytest.mark.unit

SET_AT = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
DUE_AT = SET_AT + timedelta(days=1)
# What Discord is handed for each, in whole seconds since the epoch.
SET_STAMP = int(SET_AT.timestamp())
DUE_STAMP = int(DUE_AT.timestamp())

MEMBER = 4040
AUTHOR = 3030


def reminder(
    *, set_by: int = MEMBER, message: str | None = "look at the river again", late: bool = False
) -> Panel:
    return format_reminder(
        member_id=MEMBER,
        set_by=set_by,
        set_at=SET_AT,
        due_at=DUE_AT,
        message=message,
        late=late,
    )


class TestWhoItNames:
    def test_your_own_says_you_asked_for_it(self) -> None:
        said = reminder()

        assert [block.text for block in said.blocks[:2]] == [
            "### ⏰ Reminder",
            f"<@{MEMBER}> — you asked for this <t:{SET_STAMP}:R>.",
        ]

    def test_one_somebody_else_set_says_who(self) -> None:
        said = reminder(set_by=AUTHOR)

        assert said.blocks[1].text == f"<@{MEMBER}> — <@{AUTHOR}> asked for this <t:{SET_STAMP}:R>."

    def test_the_person_it_is_for_leads_the_block_under_the_heading(self) -> None:
        """The mention is what rings them, and a panel over budget drops blocks from the end."""
        said = reminder(set_by=AUTHOR, message="x" * 500, late=True)

        assert said.blocks[1].kind is BlockKind.SUBHEADING
        assert said.blocks[1].text.startswith(f"<@{MEMBER}>")


class TestWhatItSays:
    def test_the_message_is_a_block_of_its_own(self) -> None:
        body = reminder().blocks[2]

        assert body.kind is BlockKind.BODY
        assert body.text == "look at the river again"

    @pytest.mark.parametrize("message", [None, ""])
    def test_nothing_to_say_leaves_the_block_out(self, message: str | None) -> None:
        said = reminder(message=message)

        assert [block.kind for block in said.blocks] == [BlockKind.HEADING, BlockKind.SUBHEADING]

    def test_nothing_typed_can_ring_anybody(self) -> None:
        """Posted for the whole channel, so a mention typed into the message must reach nobody:
        not a person, not a role, not a channel, not everyone."""
        body = reminder(message="<@7> <@!7> <@&7> <#7> @everyone @here").blocks[2].text

        for live in ("<@7>", "<@!7>", "<@&7>", "<#7>", "@everyone", "@here"):
            assert live not in body

    def test_markup_typed_is_shown_rather_than_obeyed(self) -> None:
        """Nothing in it can dress itself up as something this bot said."""
        body = reminder(message="### Approved\n**merge it**").blocks[2].text

        assert not body.startswith("###")
        assert "**merge it**" not in body

    def test_it_is_blue_like_everything_said_on_somebodys_behalf(self) -> None:
        assert reminder().accent is Accent.SAID


class TestWhenItIsLate:
    def test_a_reminder_on_time_says_nothing_about_time(self) -> None:
        assert all(block.kind is not BlockKind.FOOTNOTE for block in reminder().blocks)

    def test_a_late_one_says_when_it_was_due(self) -> None:
        last = reminder(late=True).blocks[-1]

        assert last.kind is BlockKind.FOOTNOTE
        assert last.text == (f"-# This was due <t:{DUE_STAMP}:R>, and could not be sent until now.")


class TestHowLongItCanGet:
    def test_the_longest_message_still_fits_and_keeps_everything(self) -> None:
        """Five hundred characters that each escape to two, which is as long as a message can
        come out, beside every other block a reminder can carry."""
        said = reminder(set_by=AUTHOR, message="_" * 500, late=True)

        assert said.length() <= PANEL_BUDGET
        assert [block.kind for block in said.blocks] == [
            BlockKind.HEADING,
            BlockKind.SUBHEADING,
            BlockKind.BODY,
            BlockKind.FOOTNOTE,
        ]


class TestARelativeTime:
    def test_it_is_discords_relative_form(self) -> None:
        assert as_relative_time(SET_AT) == f"<t:{SET_STAMP}:R>"

    def test_a_time_with_no_zone_is_read_as_utc_rather_than_the_hosts(self) -> None:
        assert as_relative_time(SET_AT.replace(tzinfo=None)) == f"<t:{SET_STAMP}:R>"
