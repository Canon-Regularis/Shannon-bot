"""The labels a repository has, remembered for a little while.

Issue #104. Two things need this list and one of them needs it fast. The command checks a typed
name against it before writing, because GitHub creates a label it has never seen rather than
refusing and a typo would add one to the repository for good. The picker beside that field reads
it on every keystroke, and Discord allows an autocomplete about three seconds to answer.

So it is cached per repository. Ten characters typed is one call rather than ten, and a taxonomy
does not change between two keystrokes. Short enough that a label added on GitHub shows up in the
picker within the minute, which is the only thing the staleness costs.

The clock is injected for the same reason it is everywhere else here: a test that has to sleep to
prove an expiry is a test nobody runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)

# Long enough that typing a name is one call, short enough that a label made on GitHub a moment
# ago can be used here without anybody wondering why it is missing.
LIFETIME = timedelta(minutes=2)


class ListsLabels(Protocol):
    """Which labels a repository has, which is all this needs of GitHub."""

    async def list_labels(self, owner: str, name: str) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class _Remembered:
    names: tuple[str, ...]
    until: datetime


class RepositoryLabels:
    """Answers what a repository's labels are, asking GitHub no more than it has to."""

    def __init__(
        self,
        github: ListsLabels,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = LIFETIME,
    ) -> None:
        self._github = github
        self._now = now
        self._lifetime = lifetime
        self._held: dict[tuple[str, str], _Remembered] = {}

    async def names(self, owner: str, name: str) -> tuple[str, ...]:
        """Every label on the repository, as GitHub spells them."""
        key = (owner.casefold(), name.casefold())
        remembered = self._held.get(key)
        now = self._now()
        if remembered is not None and remembered.until > now:
            return remembered.names

        found = tuple(await self._github.list_labels(owner, name))
        self._held[key] = _Remembered(names=found, until=now + self._lifetime)
        return found

    async def spelled(self, owner: str, name: str, wanted: str) -> str | None:
        """The repository's own spelling of this label, or None if it has no such label.

        Its spelling rather than the one that was typed, which matters more than it looks. GitHub
        matches a label name without regard to case, so writing `Bug` onto a repository that has
        `bug` attaches the label it already had while the block, which compares case-folded, sees
        no change. The command then reports something that did not happen.
        """
        asked = wanted.strip().casefold()
        for held in await self.names(owner, name):
            if held.casefold() == asked:
                return held
        return None
