from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT, clipped
from shannon.discord_bot.threads import KnowsItsServers, PostsToThread
from shannon.domain.errors import ItemNotReadyError, PermanentError
from shannon.domain.json import JsonObject
from shannon.domain.models import ItemNote
from shannon.github.mentions import names_in
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)

# Two mappings rather than one: a team slug that matches a login is not that person, and
# Discord writes the two with different syntax.
Renderer = Callable[[ItemNote, Mapping[str, int], Mapping[str, int]], Panel]
# Rebuilding reads the item from GitHub and puts it through the ordinary sync.
Rebuild = Callable[[ItemNote], Awaitable[None]]
NoteParser = Callable[[str, JsonObject], ItemNote | None]
# Optional: only one of the three mirrors carries a note that can arrive meaning nothing.
WorthPosting = Callable[[ItemNote], bool]
Follow = Callable[[ItemNote], Awaitable[None]]


class MirrorsNotes(Protocol):
    async def mirror(self, snapshot: ItemNote) -> bool: ...


class PostsAndKnowsServers(PostsToThread, KnowsItsServers, Protocol):
    """What this path needs of Discord: the post, and whether the server is still there."""


@dataclass(frozen=True, slots=True)
class _NoteTarget:
    tracked_item_id: int
    thread_id: int
    mentions: Mapping[str, int]
    roles: Mapping[str, int]
    # People only: a role mention reaches everybody holding the role, and Discord offers no way
    # to leave one person out of one.
    notify: tuple[int, ...]
    guild_id: int


class ItemNoteMirror:
    """Posts comments and reviews into the thread of whatever they were left on."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsAndKnowsServers,
        *,
        render: Renderer,
        rebuild: Rebuild | None = None,
        shut_again: KeepsThreadsShut,
        worth_posting: WorthPosting | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._render = render
        self._rebuild = rebuild
        self._shut_again = shut_again
        self._worth_posting = worth_posting

    async def mirror(self, snapshot: ItemNote) -> bool:
        """Post the note, returning whether it belonged to anything mirrored here.

        True also covers deciding the note was not worth posting. That check runs after the
        thread is found, so an untracked item still answers `ignored` rather than `processed`.
        """
        try:
            target = await self._find_thread(snapshot)
            if target is None:
                return False
            if self._worth_posting is not None and not self._worth_posting(snapshot):
                logger.info(
                    "a note on %s#%s has earned no message of its own, so none is posted",
                    snapshot.repository.full_name,
                    snapshot.item_number,
                )
                # No claim is taken, so a later decision to post these would replay them all
                # rather than find them recorded as mirrored.
                return True
            return await self._post(snapshot, target)
        except ItemNotReadyError:
            # Both ways of having nowhere to post arrive here: a thread never built, and one
            # deleted between read and post. The second branch clears the dead pointer as it
            # goes, so asking only there would let one failed rebuild end the item's mirror.
            # This sits outside both sessions, so no connection is held while GitHub is read.
            await self._ask_for_a_rebuild(snapshot)
            raise

    async def _find_thread(self, snapshot: ItemNote) -> _NoteTarget | None:
        """Find where a note should be posted, or `None` to end the delivery for good.

        A tracked item with no thread yet raises `ItemNotReadyError`, so the delivery is retried.
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

            # Read from the very string the renderer swaps names in, not the body it came from.
            # The preview is cut mid-word before the escaping, so the raw body would name
            # `monalisa` where the renderer is handed `mona`.
            named = names_in(clipped(snapshot.body, limit=COMMENT_PREVIEW_LIMIT))

            # The author last: `resolve_many` lowercases into a fresh mapping, so the last entry
            # for a name wins and the author's is the one carrying a GitHub id. That id is what
            # the changed-hands check runs on, so a self-mention must not take the unverified one.
            people: dict[str, int | None] = dict.fromkeys(named.people, None)
            if snapshot.author:
                people[snapshot.author.login] = snapshot.author.github_user_id

            links = UserLinkStore(session)
            mentions = await links.resolve_many(guild_id=repository.discord_guild_id, people=people)
            # No empty-mapping guard: both stores answer one without asking the database.
            roles = await TeamLinkStore(session).resolve_many(
                guild_id=repository.discord_guild_id,
                people=dict.fromkeys(named.teams, None),
            )
            # The same map the renderer swaps names in, so the allow-list covers both halves of
            # a note: the author in the header line and every `@login` in the quoted body.
            notify = await MutedMemberStore(session).may_be_pinged(
                guild_id=repository.discord_guild_id, ids=mentions.values()
            )
            return _NoteTarget(
                tracked_item_id=item.id,
                thread_id=item.discord_thread_id,
                mentions=mentions,
                roles=roles,
                notify=notify,
                guild_id=repository.discord_guild_id,
            )

    async def _post(self, snapshot: ItemNote, target: _NoteTarget) -> bool:
        # Claimed before the post, not recorded after it. The queue is at-least-once: a delivery
        # whose status could not be written comes back when the lease runs out and is handled
        # from the top, and recording afterwards put the same comment in the thread twice.
        if not await self._claim(target.tracked_item_id, snapshot.note_key):
            logger.info(
                "a note on %s#%s is already in its thread, not posting it again",
                snapshot.repository.full_name,
                snapshot.item_number,
            )
            return True

        try:
            await self._threads.post(
                thread_id=target.thread_id,
                panel=self._render(snapshot, target.mentions, target.roles),
                notify=target.notify,
            )
        except ThreadNotFoundError as error:
            # Only the item's own sync can open a replacement; it has the channel and the
            # metadata. Letting go of the dead id lets a late item event do it too: `_resolve`
            # turns a stale delivery away, but only for an item that still has a thread.
            await self._hand_back(target.tracked_item_id, snapshot.note_key)
            await self._forget_thread(target.tracked_item_id, target.thread_id)
            raise ItemNotReadyError(
                f"thread {target.thread_id} for {snapshot.repository.full_name}"
                f"#{snapshot.item_number} is gone and has to be rebuilt"
            ) from error
        except PermanentError:
            # A removed bot reads as a permission refusal: discord.py drops its threads from the
            # cache, so a fetch answers exactly as it does for a thread this bot may not touch.
            # A permanent failure is dropped on its first attempt and comments are never re-read,
            # so telling the two apart keeps an absence its sixteen attempts over two hours.
            await self._hand_back(target.tracked_item_id, snapshot.note_key)
            if self._threads.is_in(target.guild_id):
                raise
            raise DiscordGatewayError(
                f"this bot is not in server {target.guild_id} at the moment, so the note on "
                f"{snapshot.repository.full_name}#{snapshot.item_number} could not be posted"
            ) from None
        except BaseException:
            # Nothing was said, so the claim has to go back or the retry reads it as posted and
            # the note is lost. Cancellation counts, hence BaseException: the worker cancels a
            # delivery on its deadline, and discord.py sleeps through a rate limit here.
            await self._hand_back(target.tracked_item_id, snapshot.note_key)
            raise

        # Posting reopened the thread: Discord takes no message into an archived one, and people
        # go on commenting after an item is closed.
        await self._shut_again.again(
            tracked_item_id=target.tracked_item_id, thread_id=target.thread_id
        )

        logger.info("mirrored a note on %s#%s", snapshot.repository.full_name, snapshot.item_number)
        return True

    async def _claim(self, tracked_item_id: int, note_key: str) -> bool:
        async with self._sessionmaker() as session, session.begin():
            return await MirroredNoteStore(session).claim(tracked_item_id, note_key)

    async def _ask_for_a_rebuild(self, snapshot: ItemNote) -> None:
        """Ask for the thread to be rebuilt, best effort: the note is retried either way.

        Raising would replace a reason naming the thread with whatever went wrong reading GitHub.
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
        """Give back the claim, shielded so a cancellation mid-flight cannot interrupt it.

        Swallowed because the delivery is already failing on the error that says why. Closing the
        gap properly needs a delivery id on the row, so a retry can tell its own claim from another.
        """
        try:
            await asyncio.shield(self._release(tracked_item_id, note_key))
        except Exception:
            logger.error(
                "could not give back the claim on %s for tracked item %s. It is recorded as "
                "mirrored and was never posted, so the retry will report it done and the comment "
                "is lost; remove that row from mirrored_notes to have it posted again",
                note_key,
                tracked_item_id,
                exc_info=True,
            )

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

    `then` runs before the post. A submitted review is the only note that means something beyond
    its own text: it closes the request that asked for it.
    """

    async def handle(
        action: str, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        snapshot = parse(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED

        # The database work first, the Discord post last. A retry re-runs the whole handler and
        # posting cannot be undone, while closing a review request twice costs nothing.
        if then is not None:
            await then(snapshot)

        posted = await mirror.mirror(snapshot)
        return WebhookOutcome.PROCESSED if posted else WebhookOutcome.IGNORED

    return handle
