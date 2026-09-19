"""Which Discord messages belong in a transcript.

Issue #103. Every rule here is a skip, and each one is its own statement so a mutation of any of
them has a test of its own to go red.
"""

from __future__ import annotations

import pytest
from discord import MessageType

from shannon.db.models import DISPLAY_NAME_WIDTH, TRANSCRIPT_LINE_WIDTH
from shannon.discord_bot.capture import captured, from_a_person, has_words
from tests.fakes.discord_objects import (
    FakeAuthor,
    FakeGuild,
    FakeMentioned,
    FakeNamed,
    a_message,
)

pytestmark = pytest.mark.unit

# Snowflakes, which are fifteen to twenty digits and are what the pattern matches on.
ALICE = 111111111111111111
BOB = 222222222222222222
ROLE = 333333333333333333
CHANNEL = 444444444444444444


class TestWhoSaidIt:
    def test_a_person_is_captured(self) -> None:
        assert from_a_person(a_message()) is True

    def test_a_bot_is_not(self) -> None:
        """This bot's own mirrored GitHub comments live in the threads being captured, so without
        this every comment arriving from GitHub is transcribed straight back to GitHub, carrying
        the round before it each time."""
        assert from_a_person(a_message(author=FakeAuthor(bot=True))) is False

    def test_a_webhook_is_not(self) -> None:
        """Surer than the flag above: a webhook post carries a synthetic author that does not
        always read as a bot."""
        assert from_a_person(a_message(webhook_id=123)) is False

    @pytest.mark.parametrize(
        "kind",
        [MessageType.thread_created, MessageType.pins_add, MessageType.new_member],
    )
    def test_discord_narrating_is_not(self, kind: MessageType) -> None:
        """ "X started a thread" is furniture, not something somebody said."""
        assert from_a_person(a_message(type=kind)) is False

    def test_a_reply_is(self) -> None:
        """The other kind that is a person talking."""
        assert from_a_person(a_message(type=MessageType.reply)) is True


class TestWhetherThereIsAnythingToWriteDown:
    def test_words(self) -> None:
        assert has_words(a_message(content="hello")) is True

    @pytest.mark.parametrize("said", ["", "   ", "\n\t"])
    def test_nothing(self, said: str) -> None:
        """An attachment on its own, a sticker on its own and a poll all arrive like this. So does
        a message content intent granted in name only, which is why the caller says so once."""
        assert has_words(a_message(content=said)) is False


class TestWhatIsKept:
    def test_the_plain_values(self) -> None:
        message = captured(a_message())

        assert (message.thread_id, message.message_id, message.author_id) == (9001, 501, 77)
        assert message.author_display_name == "alice"
        assert message.content == "hello"

    def test_discords_own_clock_is_kept(self) -> None:
        """Not ours. It is what the rendered line is stamped with, and what the quiet gap is
        measured against, so a publish held up by an outage does not read as a quiet thread."""
        said = a_message()

        assert captured(said).said_at == said.created_at

    def test_surrounding_whitespace_goes(self) -> None:
        assert captured(a_message(content="  hello  ")).content == "hello"

    def test_a_long_message_is_cut_to_what_the_column_holds(self) -> None:
        said = captured(a_message(content="x" * (TRANSCRIPT_LINE_WIDTH + 50)))

        assert len(said.content) == TRANSCRIPT_LINE_WIDTH

    def test_a_long_display_name_is_cut_too(self) -> None:
        long = FakeAuthor(display_name="n" * (DISPLAY_NAME_WIDTH + 50))

        assert len(captured(a_message(author=long)).author_display_name) == DISPLAY_NAME_WIDTH


class TestWhatSomebodyTagged:
    """Issue #121. A tag has to survive with its id, because the id is the only thing `/link`
    knows anybody by and `clean_content` threw it away.

    `message.mentions` is the authority and the text is only a pointer into it. Most of these are
    about that being true rather than nearly true.
    """

    def test_a_tag_keeps_the_id_and_says_who_it_is(self) -> None:
        said = captured(
            a_message(
                content=f"hey <@{ALICE}> look",
                mentions=[FakeMentioned(id=ALICE, display_name="Alice")],
            )
        )

        assert said.content == f"hey <@{ALICE}> look"
        assert said.mentions == {ALICE: "Alice"}

    def test_the_nickname_form_is_normalised_to_one_shape(self) -> None:
        """Discord writes both and the render should have one token to look for."""
        said = captured(
            a_message(
                content=f"<@!{ALICE}> and <@{ALICE}>",
                mentions=[FakeMentioned(id=ALICE, display_name="Alice")],
            )
        )

        assert said.content == f"<@{ALICE}> and <@{ALICE}>"
        assert said.mentions == {ALICE: "Alice"}

    def test_an_id_discord_did_not_read_as_a_mention_is_nobody(self) -> None:
        """The forgery gate. Typing `<@id>` in Discord IS mentioning that person, so it turns up
        in `message.mentions` and there is nothing to be had by typing it rather than clicking a
        name. An id that is NOT in that list was never a mention, and is not made into one here.
        """
        said = captured(a_message(content=f"hey <@{BOB}> look", mentions=[]))

        assert said.content == "hey @deleted-user look"
        assert said.mentions == {}

    def test_a_reply_does_not_tag_the_person_it_answers(self) -> None:
        """Discord puts the replied-to author in `message.mentions` with no token anywhere in the
        text. Taking that list whole would tag somebody on GitHub for every reply in a thread, so
        the map is built from the substitutions actually made.
        """
        said = captured(
            a_message(content="agreed", mentions=[FakeMentioned(id=ALICE, display_name="Alice")])
        )

        assert said.content == "agreed"
        assert said.mentions == {}

    def test_two_tags_of_one_person_are_one_entry(self) -> None:
        said = captured(
            a_message(
                content=f"<@{ALICE}> and <@{ALICE}>",
                mentions=[FakeMentioned(id=ALICE, display_name="Alice")],
            )
        )

        assert said.mentions == {ALICE: "Alice"}

    def test_a_long_display_name_is_cut_to_what_the_column_holds(self) -> None:
        said = captured(
            a_message(
                content=f"<@{ALICE}>",
                mentions=[FakeMentioned(id=ALICE, display_name="n" * (DISPLAY_NAME_WIDTH + 50))],
            )
        )

        assert len(said.mentions[ALICE]) == DISPLAY_NAME_WIDTH


class TestWhatIsNotAboutPeople:
    """Roles and channels resolve as they always did. Issue #121 is about people, and this is the
    one place reimplementing discord.py's transform could have regressed something out of scope."""

    def test_a_role_is_still_its_name(self) -> None:
        guild = FakeGuild(roles={ROLE: FakeNamed(id=ROLE, name="backend")})

        said = captured(a_message(content=f"ask <@&{ROLE}>", guild=guild))

        assert said.content == "ask @backend"
        assert said.mentions == {}

    def test_a_role_nobody_knows_reads_the_way_discord_py_writes_it(self) -> None:
        assert captured(a_message(content=f"ask <@&{ROLE}>")).content == "ask @deleted-role"

    def test_a_channel_is_still_its_name(self) -> None:
        guild = FakeGuild(channels={CHANNEL: FakeNamed(id=CHANNEL, name="general")})

        said = captured(a_message(content=f"see <#{CHANNEL}>", guild=guild))

        assert said.content == "see #general"

    def test_a_channel_nobody_knows_reads_the_way_discord_py_writes_it(self) -> None:
        assert captured(a_message(content=f"see <#{CHANNEL}>")).content == "see #deleted-channel"

    def test_outside_a_server_neither_resolves(self) -> None:
        """The type says a message can have no guild. A transcript is not worth raising over in
        the handler that runs for every message in every server, so it answers instead."""
        said = captured(a_message(content=f"<@&{ROLE}> in <#{CHANNEL}>", guild=None))

        assert said.content == "@deleted-role in #deleted-channel"

    def test_a_mass_mention_is_left_for_the_github_side_to_defuse(self) -> None:
        """discord.py's `clean_content` ends by defusing these for Discord. Nothing sends this
        text back to Discord, and `github.safe_text` covers the one way it travels."""
        assert captured(a_message(content="@everyone look")).content == "@everyone look"
