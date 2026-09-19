"""Which Discord messages belong in a transcript.

Issue #103. Every rule here is a skip, and each one is its own statement so a mutation of any of
them has a test of its own to go red.
"""

from __future__ import annotations

import pytest
from discord import MessageType

from shannon.db.models import DISPLAY_NAME_WIDTH, TRANSCRIPT_LINE_WIDTH
from shannon.discord_bot.capture import captured, from_a_person, has_words
from tests.fakes.discord_objects import FakeAuthor, a_message

pytestmark = pytest.mark.unit


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
        assert has_words(a_message(clean_content="hello")) is True

    @pytest.mark.parametrize("said", ["", "   ", "\n\t"])
    def test_nothing(self, said: str) -> None:
        """An attachment on its own, a sticker on its own and a poll all arrive like this. So does
        a message content intent granted in name only, which is why the caller says so once."""
        assert has_words(a_message(clean_content=said)) is False


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
        assert captured(a_message(clean_content="  hello  ")).content == "hello"

    def test_a_long_message_is_cut_to_what_the_column_holds(self) -> None:
        said = captured(a_message(clean_content="x" * (TRANSCRIPT_LINE_WIDTH + 50)))

        assert len(said.content) == TRANSCRIPT_LINE_WIDTH

    def test_a_long_display_name_is_cut_too(self) -> None:
        long = FakeAuthor(display_name="n" * (DISPLAY_NAME_WIDTH + 50))

        assert len(captured(a_message(author=long)).author_display_name) == DISPLAY_NAME_WIDTH
