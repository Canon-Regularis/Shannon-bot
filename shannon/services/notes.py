from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import ThreadNotFoundError
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.errors import ItemNotReadyError
from shannon.domain.models import ItemNote
from shannon.github.webhooks.events import EventHandler, WebhookOutcome

logger = logging.getLogger(__name__)

Renderer = Callable[[ItemNote, Mapping[str, int]], str]
# Getting an item's thread built again, for the one case this path can detect and not mend.
# A callable rather than a service, because what it needs is the item read from GitHub and
# put through the ordinary sync, and this module has no business knowing either of those.
Rebuild = Callable[[ItemNote], Awaitable[None]]
NoteParser = Callable[[str, Mapping[str, Any]], ItemNote | None]
Follow = Callable[[ItemNote], Awaitable[None]]


class MirrorsNotes(Protocol):
    """Putting one note in its thread, which is all the handler asks for.

    The item handler already takes `SyncsItems` rather than the service that satisfies it; this
    is the same seam on the other path.
    """

    async def mirror(self, snapshot: ItemNote) -> bool: ...


@dataclass(frozen=True, slots=True)
class _NoteTarget:
    """The thread a note goes into, and who to mention in it."""

    tracked_item_id: int
    thread_id: int
    mentions: Mapping[str, int]


class ItemNoteMirror:
    """Posts comments and reviews into the thread of whatever they were left on.

    Finding the thread is the same work for both, and for pull requests and issues alike, so
    only the rendering is injected.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: PostsToThread,
        *,
        render: Renderer,
        rebuild: Rebuild | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._render = render
        self._rebuild = rebuild

    async def mirror(self, snapshot: ItemNote) -> bool:
        """Post the note, returning whether there was anywhere to post it."""
        try:
            target = await self._find_thread(snapshot)
            if target is None:
                return False
            return await self._post(snapshot, target)
        except ItemNotReadyError:
            # Both ways of having nowhere to post arrive here: an item whose thread was never
            # built, and one whose thread was deleted between the read and the post. Asking on
            # the way out is what makes the ask repeat.
            #
            # It used to sit in the second branch alone, which is the one branch that cannot be
            # reached twice: it clears the dead pointer on its way past, so every later attempt
            # of this note, and every later note on the item, stopped at the first branch
            # instead, where nothing asked for anything. One rebuild that failed for any reason
            # ended the item's mirror, and a spent rate limit or the delivery deadline landing
            # inside it is reason enough.
            #
            # Outside the sessions the two branches open, so nothing holds a connection while
            # GitHub is read.
            await self._ask_for_a_rebuild(snapshot)
            raise

    async def _find_thread(self, snapshot: ItemNote) -> _NoteTarget | None:
        """Which thread this note belongs in, or None if the item is not mirrored here.

        Raises `ItemNotReadyError` for an item that is tracked but has no thread yet, so the
        delivery is retried. Answering "nothing to do" would lose the note for good, because
        nothing ever revisits a delivery that said that.
        """
        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                logger.info(
                    "a note arrived for %s, which is not registered to any guild",
                    snapshot.repository.full_name,
                )
                return None

            # By number, not by id. A pull request reports its issue id in comment payloads,
            # which never matches the pull request id stored against the tracked item.
            item = await TrackedItemStore(session).get_by_number(
                repository_id=repository.id,
                number=snapshot.item_number,
                object_type=snapshot.object_type,
            )
            if item is None:
                logger.info(
                    "note on %s#%s is not tracked here, ignoring",
                    snapshot.repository.full_name,
                    snapshot.item_number,
                )
                return None

            if item.discord_thread_id is None:
                raise ItemNotReadyError(
                    f"{snapshot.repository.full_name}#{snapshot.item_number} has no thread yet"
                )

            mentions = (
                await UserLinkStore(session).resolve_many(
                    guild_id=repository.discord_guild_id,
                    github_usernames=[snapshot.author.login],
                )
                if snapshot.author
                else {}
            )
            return _NoteTarget(
                tracked_item_id=item.id,
                thread_id=item.discord_thread_id,
                mentions=mentions,
            )

    async def _post(self, snapshot: ItemNote, target: _NoteTarget) -> bool:
        """Put the note in its thread, once."""
        # Claimed before the post, not recorded after it. The queue is at-least-once by design:
        # a delivery whose status could not be written stays leased, comes back when the lease
        # runs out, and is handled again from the top. Recording afterwards leaves that same gap
        # one step further along, and the gap put the same comment in the thread twice.
        if not await self._claim(target.tracked_item_id, snapshot.note_key):
            logger.info(
                "a note on %s#%s is already in its thread, not posting it again",
                snapshot.repository.full_name,
                snapshot.item_number,
            )
            return True

        try:
            await self._threads.post(
                thread_id=target.thread_id, content=self._render(snapshot, target.mentions)
            )
        except ThreadNotFoundError as error:
            # Only the item's own sync knows how to open a replacement, because only it has the
            # channel and the metadata. Letting go of the dead id is what lets an item event
            # that arrived late do it too: `_resolve` turns a stale delivery away, but only for
            # an item that still has a thread to show for itself.
            #
            # Asking for the rebuild is the other half, and `mirror` does that, for this branch
            # and for the one that finds no thread at all.
            await self._hand_back(target.tracked_item_id, snapshot.note_key)
            await self._forget_thread(target.tracked_item_id, target.thread_id)
            raise ItemNotReadyError(
                f"thread {target.thread_id} for {snapshot.repository.full_name}"
                f"#{snapshot.item_number} is gone and has to be rebuilt"
            ) from error
        except BaseException:
            # Nothing was said, so the claim has to go back or the retry reads it as already
            # posted and the note is lost for good. Cancellation counts as a failure here, which
            # is why this catches everything: the worker puts a deadline on each delivery and
            # cancels the handler where it stands, and discord.py sleeps through a rate limit
            # rather than failing, so where it stands is often exactly here.
            await self._hand_back(target.tracked_item_id, snapshot.note_key)
            raise

        logger.info("mirrored a note on %s#%s", snapshot.repository.full_name, snapshot.item_number)
        return True

    async def _claim(self, tracked_item_id: int, note_key: str) -> bool:
        async with self._sessionmaker() as session, session.begin():
            return await MirroredNoteStore(session).claim(tracked_item_id, note_key)

    async def _ask_for_a_rebuild(self, snapshot: ItemNote) -> None:
        """Get the item's thread built again, best effort.

        Best effort on purpose. The note is going to be retried either way, so a rebuild that
        cannot happen now may well work on the attempt after, and raising from here would replace
        a reason that names the thread with whatever went wrong reading GitHub.

        Optional, because a mirror with nothing wired in still behaves as it did: the note is
        retried and the thread waits for an item event. Only the wiring decides.
        """
        if self._rebuild is None:
            return
        try:
            await self._rebuild(snapshot)
        except Exception:
            logger.warning(
                "could not rebuild the thread for %s#%s; the note will be tried again",
                snapshot.repository.full_name,
                snapshot.item_number,
                exc_info=True,
            )

    async def _hand_back(self, tracked_item_id: int, note_key: str) -> None:
        """Give a claim back, best effort.

        Shielded so a cancellation mid-flight cannot interrupt the hand-back. Suppressed because
        the note stays unposted either way and the original error is the one that says why.
        """
        with contextlib.suppress(Exception):
            await asyncio.shield(self._release(tracked_item_id, note_key))

    async def _release(self, tracked_item_id: int, note_key: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await MirroredNoteStore(session).release(tracked_item_id, note_key)

    async def _forget_thread(self, tracked_item_id: int, dead_thread_id: int) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(
                tracked_item_id, dead_thread_id=dead_thread_id
            )


def build_note_handler(
    mirror: MirrorsNotes, parse: NoteParser, *, then: Follow | None = None
) -> EventHandler:
    """Adapt a comment or review webhook to the mirror.

    `then` runs once the note is in the thread. A submitted review is the only note that means
    something beyond its own text: it closes the request that asked for it.
    """

    async def handle(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        snapshot = parse(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED

        # The database work first, the Discord post last. A retry re-runs the whole handler,
        # and posting a message is the one step that cannot be undone: anything after it that
        # fails puts the same comment in the thread a second time. Closing a review request
        # twice costs nothing, so it is the half that is safe to repeat.
        if then is not None:
            await then(snapshot)

        posted = await mirror.mirror(snapshot)
        return WebhookOutcome.PROCESSED if posted else WebhookOutcome.IGNORED

    return handle
