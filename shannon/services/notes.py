from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.rich_text import as_note_text
from shannon.discord_bot.threads import KnowsItsServers, PostsToThread, RevisesMessages
from shannon.domain.errors import ItemNotReadyError, PermanentError
from shannon.domain.json import JsonObject
from shannon.domain.models import ItemNote
from shannon.github.mentions import names_a_note_writes
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.locating import in_its_thread
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
    async def mirror(self, snapshot: ItemNote, *, edited: bool = False) -> bool: ...


class PostsAndKnowsServers(PostsToThread, RevisesMessages, KnowsItsServers, Protocol):
    """What this path needs of Discord: the post, the rewrite, and whether the server is there."""


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

    async def mirror(self, snapshot: ItemNote, *, edited: bool = False) -> bool:
        """Post the note, returning whether it belonged to anything mirrored here.

        True also covers deciding the note was not worth posting. That check runs after the
        thread is found, so an untracked item still answers `ignored` rather than `processed`.

        `edited` is whether GitHub called this an edit rather than a creation (issue #165). A
        boolean through this one call rather than a field on the snapshot: all three note kinds are
        frozen dataclasses behind a Protocol of properties, so a field would have to be added to
        every one of them to say something none of them is about - what the DELIVERY was, not what
        the comment is.
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
            return await self._post(snapshot, target, edited=edited)
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
            found = await in_its_thread(
                session,
                repository=snapshot.repository,
                number=snapshot.item_number,
                object_type=snapshot.object_type,
                about="a note",
            )
            if found is None:
                return None

            # Read from the very string the renderer swaps names in, and through the very same
            # function. The preview is cut mid-word, so the raw body would name `monalisa` where
            # the renderer is handed `mona` - and since issue #166 the two differ by more than the
            # escaping, because the rich conversion strips HTML comments and image markup before it
            # cuts, so the window lands somewhere else entirely.
            #
            # One function for the text, and one for how to read it. A name that one of these
            # sees and the other does not renders exactly as an unlinked name renders, and nothing
            # anywhere reports a mention that was owed and never made - so neither the string nor
            # the rule for reading it is spelled out twice. `names_a_note_writes` is paired with
            # the `rewrite_a_note` the renderer calls.
            named = names_a_note_writes(as_note_text(snapshot.body))

            # The author last: `resolve_many` lowercases into a fresh mapping, so the last entry
            # for a name wins and the author's is the one carrying a GitHub id. That id is what
            # the changed-hands check runs on, so a self-mention must not take the unverified one.
            people: dict[str, int | None] = dict.fromkeys(named.people, None)
            if snapshot.author:
                people[snapshot.author.login] = snapshot.author.github_user_id

            links = UserLinkStore(session)
            mentions = await links.resolve_many(guild_id=found.guild_id, people=people)
            # No empty-mapping guard: both stores answer one without asking the database.
            roles = await TeamLinkStore(session).resolve_many(
                guild_id=found.guild_id, people=dict.fromkeys(named.teams, None)
            )
            # The same map the renderer swaps names in, so the allow-list covers both halves of
            # a note: the author in the header line and every `@login` in the quoted body.
            notify = await MutedMemberStore(session).may_be_pinged(
                guild_id=found.guild_id, ids=mentions.values()
            )
            return _NoteTarget(
                tracked_item_id=found.tracked_item_id,
                thread_id=found.thread_id,
                mentions=mentions,
                roles=roles,
                notify=notify,
                guild_id=found.guild_id,
            )

    async def _post(self, snapshot: ItemNote, target: _NoteTarget, *, edited: bool = False) -> bool:
        # Claimed before the post, not recorded after it. The queue is at-least-once: a delivery
        # whose status could not be written comes back when the lease runs out and is handled
        # from the top, and recording afterwards put the same comment in the thread twice.
        if not await self._claim(target.tracked_item_id, snapshot.note_key):
            # The claim already tells the two cases apart, so issue #165 needed no second guard.
            # A claim that fails means the note is in the thread, which is the one circumstance in
            # which there is a message to rewrite; a claim that succeeds means there is not, and an
            # edit of a comment that never got mirrored falls through and posts, repairing the gap.
            if edited:
                return await self._revise(snapshot, target)
            logger.info(
                "a note on %s#%s is already in its thread, not posting it again",
                snapshot.repository.full_name,
                snapshot.item_number,
            )
            return True

        try:
            message_id = await self._threads.post(
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

        # Which message it went out as, so an edit on GitHub can be mirrored onto it rather than
        # posted underneath it (issue #165). After the post and outside its handler on purpose: the
        # post has landed by now, so a failure to write this down must not undo it or re-raise. What
        # it costs is the ability to mirror a later edit of this one comment, which is why it is
        # recorded at all rather than read back from somewhere - there is nowhere to read it from.
        #
        # Written whatever came back, including None. The gateway's return is typed `int | None` and
        # every implementation of it answers with an id, so guarding here would add a branch no test
        # could reach; and None is a truthful answer anyway - a null column already means "nobody
        # knows which message holds this note", which an edit handles by saying so.
        await self._remember_the_message(
            target.tracked_item_id, snapshot.note_key, message_id=message_id
        )

        # Posting reopened the thread: Discord takes no message into an archived one, and people
        # go on commenting after an item is closed.
        await self._shut_again.again(
            tracked_item_id=target.tracked_item_id, thread_id=target.thread_id
        )

        logger.info("mirrored a note on %s#%s", snapshot.repository.full_name, snapshot.item_number)
        return True

    async def _revise(self, snapshot: ItemNote, target: _NoteTarget) -> bool:
        """Rewrite the message this note already went out as. Issue #165.

        Rendered from the CURRENT maps, which `_find_thread` built from the body this delivery
        carries - so a name that has been linked to a Discord account since the original post
        resolves now, and the edited comment shows the tag the original could not.

        Nobody is pinged, and nothing here chooses that: Discord sends no notification for an edit
        whatever the content says. It is the whole reason this is an edit rather than a replacement
        posted underneath - everybody named in the comment keeps their mention, and not one of them
        is rung again for a typo somebody fixed.

        No claim is taken and none is handed back. The claim belongs to the original post and still
        does; a redelivered edit simply writes the same content over the same message, which is why
        an edit needs no guard of its own to be safe under an at-least-once queue.
        """
        message_id = await self._message_for(target.tracked_item_id, snapshot.note_key)
        if message_id is None:
            # A note mirrored before the id was recorded. It cannot be found now - GitHub's comment
            # says nothing about which Discord message holds it - so the edit is dropped rather than
            # posted a second time under the stale one. Self-healing: notes mirrored from here on
            # all record one.
            logger.info(
                "a note on %s#%s was mirrored before its message id was kept, so an edit to it "
                "cannot be shown",
                snapshot.repository.full_name,
                snapshot.item_number,
            )
            return True

        revised = await self._threads.revise(
            thread_id=target.thread_id,
            message_id=message_id,
            panel=self._render(snapshot, target.mentions, target.roles),
        )
        if not revised:
            logger.info(
                "the message mirroring a note on %s#%s has been deleted, so an edit to it cannot "
                "be shown",
                snapshot.repository.full_name,
                snapshot.item_number,
            )
            return True

        # The rewrite reopened the thread exactly as a post does, so it is shut again the same way.
        await self._shut_again.again(
            tracked_item_id=target.tracked_item_id, thread_id=target.thread_id
        )

        logger.info(
            "mirrored an edit to a note on %s#%s",
            snapshot.repository.full_name,
            snapshot.item_number,
        )
        return True

    async def _remember_the_message(
        self, tracked_item_id: int, note_key: str, *, message_id: int | None
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            await MirroredNoteStore(session).remember_the_message(
                tracked_item_id, note_key, message_id=message_id
            )

    async def _message_for(self, tracked_item_id: int, note_key: str) -> int | None:
        async with self._sessionmaker() as session:
            return await MirroredNoteStore(session).message_for(tracked_item_id, note_key)

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
    mirror: MirrorsNotes,
    parse: NoteParser,
    *,
    then: Follow | None = None,
    after: Follow | None = None,
) -> EventHandler:
    """Adapt a comment or review webhook to the mirror.

    Two hooks, on opposite sides of the post, and which side a thing goes on is decided by what
    it is rather than by convenience.

    `then` runs before. A submitted review is the only note that means something beyond its own
    text: it closes the request that asked for it. Database work only — the mirror's post is the
    one thing here that does not need GitHub, and a hook that reached for it would make a GitHub
    outage cost the review line itself.

    `after` runs once the note is in the thread, so anything it says lands underneath the note
    rather than above it. Issue #155: a round-up saying every review has come back approving is
    read as being about the approval above it, and posted first it would be about a message
    nobody had seen yet.

    Skipped where the mirror answered False, which is a free gate rather than a courtesy: that is
    an unregistered repository or an item this server does not track, so a note on something
    nobody is watching costs no GitHub calls at all.

    What False does NOT mean is that no message went out. A note the mirror's own predicate
    declined — a `commented` review wrapping nothing but inline replies — is True, because the
    ledger behind it still ran. So `after` is reached for a note nobody can see, and a hook that
    speaks under one has to decide for itself whether it has anything to say. The one hook here
    is gated on the review being an approval, and a declined note is never one, so the two cannot
    meet; that is a coupling rather than a guarantee, which is why it is written down.
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

        # What GitHub called this delivery. `edited` is in every one of the three action sets this
        # handler serves, and means the same thing in each: the note is already in its thread and
        # its text has moved on.
        posted = await mirror.mirror(snapshot, edited=action == "edited")
        if not posted:
            return WebhookOutcome.IGNORED
        if after is not None:
            await after(snapshot)
        return WebhookOutcome.PROCESSED

    return handle
