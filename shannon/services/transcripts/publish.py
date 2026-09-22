"""Turning transcript lines into a comment on an item.

Takes the `FoundItem` that `locate` hands every command run inside a thread, not a conversation
id, so it works in a thread with no open conversation and the flusher is only one producer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from shannon.services.transcripts.lines import TranscriptLine, render
from shannon.services.workflow import FoundItem

logger = logging.getLogger(__name__)


class SaysThings(Protocol):
    """Writing a comment on an item, which is all this needs of GitHub."""

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None: ...


class TranscriptPublisher:
    """Posts a set of lines onto an item as one comment."""

    def __init__(self, github: SaysThings) -> None:
        self._github = github

    async def publish(self, found: FoundItem, lines: Sequence[TranscriptLine]) -> None:
        """Put these lines on the item as a single comment.

        One comment rather than one per line: a ten-message exchange published a line at a time
        sends everybody watching the item ten emails.
        """
        await self._github.add_comment(found.owner, found.name, found.number, render(lines))
        logger.info(
            "published %s lines from a Discord thread to %s#%s",
            len(lines),
            found.full_name,
            found.number,
        )
