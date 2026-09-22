"""Turning transcript lines into a comment on an item.

Takes the `FoundItem` that `locate` hands every command run inside a thread, not a conversation
id, so it works in a thread with no open conversation and the flusher is only one producer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from shannon.services.transcripts.lines import Relay, TranscriptLine, render
from shannon.services.workflow import FoundItem

logger = logging.getLogger(__name__)


class SaysThings(Protocol):
    """Writing a comment on an item, which is all this needs of GitHub."""

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None: ...


class TranscriptPublisher:
    """Posts a set of lines onto an item as one comment."""

    def __init__(self, github: SaysThings) -> None:
        self._github = github

    async def publish(
        self, found: FoundItem, lines: Sequence[TranscriptLine], *, thread_id: int
    ) -> None:
        """Put these lines on the item as a single comment.

        One comment rather than one per line: a ten-message exchange published a line at a time
        sends everybody watching the item ten emails.

        `thread_id` is the caller's because it is not the item's: an item has one thread now, and
        a transcript is of the thread the messages were captured in, which a relocation can have
        moved on from since.
        """
        relay = Relay(
            object_type=found.object_type,
            number=found.number,
            guild_id=found.guild_id,
            thread_id=thread_id,
        )
        body = render(relay, lines)
        await self._github.add_comment(found.owner, found.name, found.number, body)
        logger.info(
            "published %s lines from a Discord thread to %s#%s",
            len(lines),
            found.full_name,
            found.number,
        )
