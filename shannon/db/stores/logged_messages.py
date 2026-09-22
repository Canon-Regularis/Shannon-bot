"""The messages captured from a thread, waiting to be published."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import LoggedMessage


def mentioned_in(message: LoggedMessage) -> dict[int, str]:
    """Who this message tagged, by Discord id.

    The column holds a JSON object and JSON has no integer keys, so the ids go in as decimal
    strings. A key that is not one is skipped rather than raised on, since a whole transcript
    refusing to publish over one hand edit is the worse failure.
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

        Idempotent on the message id: discord.py redelivers an event after a resumed session,
        and the same message would otherwise be transcribed twice. Doing nothing on a conflict
        rather than updating is also what makes an edit a no-op.
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

        Ordered by id rather than `said_at`, so two messages sharing a timestamp still read in
        the order the thread was written in.
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

        By message id alone rather than per conversation: a raw delete event carries a channel
        and a message, and the caller has already decided the channel is one being logged.
        """
        if not message_ids:
            return
        await self._session.execute(
            delete(LoggedMessage).where(LoggedMessage.discord_message_id.in_(message_ids))
        )
