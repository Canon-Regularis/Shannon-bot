from __future__ import annotations

from dataclasses import dataclass, field

from shannon.discord_bot.errors import (
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
    ThreadStartedEmptyError,
)
from shannon.discord_bot.threads import Notify, ThreadHandle, truncate_thread_name


@dataclass
class FakeThread:
    thread_id: int
    channel_id: int
    name: str
    messages: dict[int, str] = field(default_factory=dict)
    metadata_message_id: int | None = None
    locked: bool = False
    archived: bool = False


class FakeThreadGateway:
    """ThreadGateway backed by dictionaries.

    Records enough to assert that a second webhook edited the existing thread instead of
    opening a new one.
    """

    def __init__(self) -> None:
        self.threads: dict[int, FakeThread] = {}
        self.created: list[FakeThread] = []
        self.posts: list[tuple[int, str]] = []
        # What each write said it was allowed to notify, beside what it wrote. Kept apart from
        # `posts` above, which around fifty tests read as (thread, content) and which is not worth
        # churning for this.
        #
        # None and () are different answers and the distinction is the point: None is a caller
        # with no opinion, leaving the client's own rule in force, and () is a caller saying
        # nobody. A test that asserts on one and means the other proves nothing.
        self.allowed: list[tuple[str, int, str, Notify]] = []
        # Every rewrite of an existing thread, whether or not anything about it changed. A rename
        # only records a new name, so it cannot show a thread being written twice with the same
        # content, which is what a card mirrored twice looks like.
        self.updates: list[int] = []
        self.renames: list[tuple[int, str]] = []
        # Where the thread ENDED UP, recorded only when it moved. A test asking whether a thread
        # is shut wants this one.
        self.shuts: list[tuple[int, bool]] = []
        # Every time Discord was ASKED, whether or not the answer changed anything and whether or
        # not it was refused. The two are not the same question, and a test meaning to say "this
        # cost no Discord call" cannot use the one above: shutting a thread that is already shut
        # records nothing there, so the assertion passes whether the call was made or not.
        self.shut_calls: list[tuple[int, bool]] = []
        self.deleted: list[int] = []
        self.unarchived: list[int] = []
        # Set by a test that needs the next thread creation to fail the way a Discord outage
        # would, so what happens to everything queued behind it can be observed.
        self.fail_next_create = False
        # And for posting into a thread. Its own switch because issue #103 makes a refused
        # post mean something no other one does: `/log_conversation` announces itself before
        # it arms anything, so a thread that will not take the notice is a conversation that
        # must not be captured.
        self.post_error: Exception | None = None
        # The same for shutting, which is a separate permission on Discord's side: a server can
        # let this bot open and edit threads and not let it close one.
        self.fail_next_shut = False
        # A server that will never let it close one, which is a different thing: no amount of
        # waiting grants a permission, and a caller with nobody to tell has to stop asking.
        self.refuses_every_shut = False
        # And for asking where a thread is. Its own switch because a refusal there means
        # something different from every other one: the answer is unknown rather than no, and
        # a caller that read it as no would let go of a live thread.
        self.refuses_every_lookup = False
        # Every thread whose channel was asked about, so a test can say a second run asked
        # nothing because the first wrote the answer down.
        self.lookups: list[int] = []
        # And for rewriting a thread that already exists, which is what a card that moves after
        # its first mirror needs.
        self.fail_next_update = False
        # A server that will never accept a rewrite, which is what a deleted channel, a bot
        # removed from the guild and a revoked permission all look like from here.
        self.refuses_every_update = False
        # Servers this bot has been removed from, which is not the same as a permission it was
        # never given even though Discord answers both the same way.
        self.removed_from: set[int] = set()
        # Opening the thread and writing the first message in it are two Discord calls and two
        # permissions, so the second can be refused on its own. The thread is real by then, and
        # the real gateway hands its id back with the failure so the row can point at it.
        self.fail_next_first_message = False
        self._next_id = 1000

    def _allocate(self) -> int:
        self._next_id += 1
        return self._next_id

    async def create(
        self, *, channel_id: int, name: str, content: str, notify: Notify = None
    ) -> ThreadHandle:
        if self.fail_next_create:
            self.fail_next_create = False
            raise DiscordGatewayError("Discord refused to create a thread")

        thread_id = self._allocate()
        message_id = self._allocate()
        thread = FakeThread(
            thread_id=thread_id,
            channel_id=channel_id,
            name=truncate_thread_name(name),
            messages={} if self.fail_next_first_message else {message_id: content},
            metadata_message_id=None if self.fail_next_first_message else message_id,
        )
        self.threads[thread_id] = thread
        self.created.append(thread)
        self.allowed.append(("create", thread_id, content, notify))
        if self.fail_next_first_message:
            self.fail_next_first_message = False
            raise ThreadStartedEmptyError(
                "Discord refused to post the first message", thread_id=thread_id
            )
        return ThreadHandle(thread_id=thread_id, message_id=message_id)

    async def update(
        self,
        *,
        thread_id: int,
        message_id: int | None,
        name: str,
        content: str,
        notify: Notify = None,
    ) -> ThreadHandle:
        if self.refuses_every_update:
            raise DiscordPermissionError("Discord will not let the bot write in that channel")
        if self.fail_next_update:
            self.fail_next_update = False
            raise DiscordGatewayError("Discord refused to update the thread")

        thread = self._wake(thread_id)
        self.updates.append(thread_id)
        self.allowed.append(("update", thread_id, content, notify))

        wanted = truncate_thread_name(name)
        if thread.name != wanted:
            thread.name = wanted
            self.renames.append((thread_id, wanted))

        if message_id is None or message_id not in thread.messages:
            message_id = self._allocate()
        thread.messages[message_id] = content
        thread.metadata_message_id = message_id
        return ThreadHandle(thread_id=thread_id, message_id=message_id)

    def is_in(self, guild_id: int) -> bool:
        """In every server, unless a test says otherwise. Set False to stand for a bot that has
        been removed, which Discord reports as the same refusal as a missing permission."""
        return guild_id not in self.removed_from

    async def set_shut(self, *, thread_id: int, shut: bool) -> None:
        self.shut_calls.append((thread_id, shut))
        if self.refuses_every_shut:
            raise DiscordPermissionError("Discord will not let the bot close the thread")
        if self.fail_next_shut:
            self.fail_next_shut = False
            raise DiscordGatewayError("Discord refused to close the thread")
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        if thread.locked == shut and thread.archived == shut:
            return
        # One edit setting both, the way the real gateway does it. Moving them together here is
        # what makes a test able to catch a caller that leaves a thread archived and unlocked,
        # which is the pairing that quietly reopens on the next reply.
        thread.archived = shut
        if thread.locked != shut:
            thread.locked = shut
            self.shuts.append((thread_id, shut))

    async def channel_of(self, *, thread_id: int) -> int | None:
        """Where a thread is, from the channel it was created in.

        The fake already records that and never changes it, which is the real constraint: a
        thread's channel is fixed at birth. A thread nobody has answers None rather than raising,
        the way the real gateway does.
        """
        self.lookups.append(thread_id)
        if self.refuses_every_lookup:
            raise DiscordPermissionError("Discord will not let the bot see that thread")
        thread = self.threads.get(thread_id)
        return None if thread is None else thread.channel_id

    async def post(self, *, thread_id: int, content: str, notify: Notify = None) -> int | None:
        if self.post_error is not None:
            raise self.post_error
        thread = self._wake(thread_id)
        message_id = self._allocate()
        thread.messages[message_id] = content
        self.posts.append((thread_id, content))
        self.allowed.append(("post", thread_id, content, notify))
        return message_id

    async def delete(self, *, thread_id: int) -> None:
        thread = self.threads.pop(thread_id, None)
        if thread is not None:
            self.deleted.append(thread_id)

    def _wake(self, thread_id: int) -> FakeThread:
        """Find a thread, unarchiving it the way the real gateway does before it writes.

        Discord rejects writes to an archived thread, and archives one on its own once it goes
        quiet, so every write path reopens it first.
        """
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        if thread.archived:
            thread.archived = False
            self.unarchived.append(thread_id)
        return thread

    def metadata_of(self, thread_id: int) -> str:
        thread = self.threads[thread_id]
        assert thread.metadata_message_id is not None
        return thread.messages[thread.metadata_message_id]
