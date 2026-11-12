"""Making sure a label this bot writes exists on the repository, named and coloured as it should be.

GitHub creates a label it has never seen the moment one is put on an item, in grey, which is how
every status and priority label looked before issue #246. So before this bot puts one of its own
labels on, the repository is made to hold it in the right colour, and the label it used to write
in its place - `HIGH` for `priority: High` - is renamed to it rather than left beside it. A rename
moves the label on every item carrying it at once, which is what makes the old labels go away
without anybody relabelling a thing.

Only where the repository does not have the new name yet. GitHub refuses to rename a label onto a
name it already has and has no way to merge two, so where `priority: High` exists already - made
by hand, or made grey by GitHub on a write this could not prepare for - the old label stays beside
it, is still read, and comes off each item as that item is next set.

Best effort, deliberately. A label in the wrong colour is a cosmetic fault, and refusing a status
change over one would be the wrong way round: every failure here is logged and the write it was
for carries on, with GitHub creating the label grey exactly as it did before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from shannon.domain.errors import ShannonError
from shannon.github.errors import GitHubRefusedError
from shannon.github.labels import LabelDefinition

logger = logging.getLogger(__name__)

# How long a label found right is taken to stay right. A repository's labels change rarely, and
# this bounds how long one recoloured or deleted by hand goes uncorrected.
LIFETIME = timedelta(hours=1)


class DefinesLabels(Protocol):
    """Reading a repository's labels with their colours, and making or changing one."""

    async def label_colours(self, owner: str, name: str) -> dict[str, str]: ...

    async def create_label(self, owner: str, name: str, label: str, colour: str) -> None: ...

    async def update_label(
        self, owner: str, name: str, label: str, *, new_name: str, colour: str
    ) -> None: ...


class LabelDefinitions:
    """Puts this bot's own labels on a repository, asking GitHub no more than it has to."""

    def __init__(
        self,
        github: DefinesLabels,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = LIFETIME,
    ) -> None:
        self._github = github
        self._now = now
        self._lifetime = lifetime
        # Keyed on the repository case-folded, because GitHub matches it without regard to
        # case, and on the label exactly as wanted, spelling and colour. A definition that differs
        # from the last one settled by either is asked about again rather than taken as done.
        self._settled: dict[tuple[str, str, str, str], datetime] = {}

    async def ensure(self, owner: str, name: str, definition: LabelDefinition) -> None:
        """Make the repository hold this label, in its colour, before it is put on an item.

        Never raises. See the module's docstring for why a failure here is not worth one.
        """
        key = (owner.casefold(), name.casefold(), definition.name, definition.colour.casefold())
        now = self._now()
        until = self._settled.get(key)
        if until is not None and until > now:
            return
        try:
            await self._ensure(owner, name, definition)
        except ShannonError as error:
            logger.warning(
                "could not give %s/%s the label %r in its colour, so GitHub will make it grey: %s",
                owner,
                name,
                definition.name,
                error,
            )
            return
        self._settled[key] = now + self._lifetime

    async def _ensure(self, owner: str, name: str, definition: LabelDefinition) -> None:
        held = {
            label.casefold(): (label, colour)
            for label, colour in (await self._github.label_colours(owner, name)).items()
        }
        wanted = definition.colour.casefold()

        found = held.get(definition.name.casefold())
        if found is not None:
            spelled, colour = found
            # The spelling as well as the colour, because GitHub keeps whichever case the label
            # was made in and a repository's `Priority: high` is this bot's label in all but that.
            if spelled != definition.name or colour.casefold() != wanted:
                await self._github.update_label(
                    owner, name, spelled, new_name=definition.name, colour=definition.colour
                )
            return

        # The rename in place, where the repository still has a label this one takes over from.
        for old in definition.replaces:
            legacy = held.get(old.casefold())
            if legacy is not None:
                await self._github.update_label(
                    owner, name, legacy[0], new_name=definition.name, colour=definition.colour
                )
                logger.info("renamed %r on %s/%s to %r", legacy[0], owner, name, definition.name)
                return

        try:
            await self._github.create_label(owner, name, definition.name, definition.colour)
        except GitHubRefusedError:
            # Made by somebody else between the listing and now, which GitHub answers with a 422.
            # The label is there, so all that can be wrong with it is the colour.
            await self._github.update_label(
                owner, name, definition.name, new_name=definition.name, colour=definition.colour
            )
