"""The messages captured from a thread, waiting to be published. Issue #103."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import LoggedMessage


def mentioned_in(message: LoggedMessage) -> dict[int, str]:
    """Who this message tagged, by Discord id.

    The column holds a JSON object and JSON has no integer keys, so the ids go in as
    decimal strings and come back here. A key that is not one is skipped rather than
    raised on: capture is the only writer, so any other shape could only be a hand edit,
    and a whole transcript refusing to publish over one is the worse failure.
    """
    return {int(who): name for who, name in message.mentions.items() if who.isdigit()}


class LoggedMessageStore:
    """What has been said in a logged thread and not yet reached GitHub."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        conversation_id: int,
        discord_message_id: int,
        discord_author_id: int,
        author_display_name: str,
        content: str,
        said_at: datetime,
        mentions: Mapping[int, str],
    ) -> None:
        """Keep one message until it is published.

        Idempotent on the message id, because discord.py redelivers an event after a resumed
        session and the same message would otherwise be transcribed twice. Doing nothing on a
        conflict rather than updating is what makes edits a no-op without a rule anywhere saying
        so: a second arrival of a message already held changes nothing.
        """
        await self._session.execute(
            pg_insert(LoggedMessage)
            .values(
                conversation_id=conversation_id,
                discord_message_id=discord_message_id,
                discord_author_id=discord_author_id,
                author_display_name=author_display_name,
                content=content,
                said_at=said_at,
                mentions={str(who): name for who, name in mentions.items()},
            )
            .on_conflict_do_nothing(constraint="uq_logged_messages_conversation_message")
        )

    async def through(self, conversation_id: int, through_id: int) -> Sequence[LoggedMessage]:
        """The claimed batch, oldest first.

        Ordered by id rather than by `said_at`, so the transcript reads in the order the thread
        was written in even where two messages share a timestamp.
        """
        found = await self._session.scalars(
            select(LoggedMessage)
            .where(
                LoggedMessage.conversation_id == conversation_id,
                LoggedMessage.id <= through_id,
            )
            .order_by(LoggedMessage.id)
        )
        return list(found)

    async def delete_through(self, conversation_id: int, through_id: int) -> None:
        """Drop the batch that has been published, and nothing that arrived after it."""
        await self._session.execute(
            delete(LoggedMessage).where(
                LoggedMessage.conversation_id == conversation_id,
                LoggedMessage.id <= through_id,
            )
        )

    async def forget(self, message_ids: Sequence[int]) -> None:
        """Drop messages deleted in Discord before they were published.

        Retraction rather than accuracy, which is why this exists and why editing a message does
        not change what is published. Deleting something before it goes out is a rule somebody can
        hold in their head and act on; whether an edit lands would depend on invisible timing.

        By message id alone rather than per conversation, because a raw delete event carries a
        channel and a message and the caller has already decided the channel is one being logged.
        """
        if not message_ids:
            return
        await self._session.execute(
            delete(LoggedMessage).where(LoggedMessage.discord_message_id.in_(message_ids))
        )
