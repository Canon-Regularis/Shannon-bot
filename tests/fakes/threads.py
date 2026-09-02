from __future__ import annotations

from dataclasses import dataclass, field

from shannon.discord_bot.errors import (
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
    ThreadStartedEmptyError,
)
from shannon.discord_bot.threads import ThreadHandle, truncate_thread_name


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
        # Every rewrite of an existing thread, whether or not anything about it changed. A rename
        # only records a new name, so it cannot show a thread being written twice with the same
        # content, which is what a card mirrored twice looks like.
        self.updates: list[int] = []
        self.renames: list[tuple[int, str]] = []
        # Where the lock ENDED UP, recorded only when it moved. A test asking whether a thread
        # is shut wants this one.
        self.locks: list[tuple[int, bool]] = []
        # Every time Discord was ASKED, whether or not the answer changed anything and whether or
        # not it was refused. The two are not the same question, and a test meaning to say "this
        # cost no Discord call" cannot use the one above: setting a lock to what it already is
        # records nothing there, so the assertion passes whether the call was made or not.
        self.lock_calls: list[tuple[int, bool]] = []
        self.deleted: list[int] = []
        self.unarchived: list[int] = []
        # Set by a test that needs the next thread creation to fail the way a Discord outage
        # would, so what happens to everything queued behind it can be observed.
        self.fail_next_create = False
        # The same for locking, which is a separate permission on Discord's side: a server can
        # let this bot open and edit threads and not let it close one.
        self.fail_next_lock = False
        # A server that will never let it close one, which is a different thing: no amount of
        # waiting grants a permission, and a caller with nobody to tell has to stop asking.
        self.refuses_every_lock = False
        # And for rewriting a thread that already exists, which is what a card that moves after
        # its first mirror needs.
        self.fail_next_update = False
        # A server that will never accept a rewrite, which is what a deleted channel, a bot
        # removed from the guild and a revoked permission all look like from here.
        self.refuses_every_update = False
        # Opening the thread and writing the first message in it are two Discord calls and two
        # permissions, so the second can be refused on its own. The thread is real by then, and
        # the real gateway hands its id back with the failure so the row can point at it.
        self.fail_next_first_message = False
        self._next_id = 1000

    def _allocate(self) -> int:
        self._next_id += 1
        return self._next_id

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
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
        if self.fail_next_first_message:
            self.fail_next_first_message = False
            raise ThreadStartedEmptyError(
                "Discord refused to post the first message", thread_id=thread_id
            )
        return ThreadHandle(thread_id=thread_id, message_id=message_id)

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle:
        if self.refuses_every_update:
            raise DiscordPermissionError("Discord will not let the bot write in that channel")
        if self.fail_next_update:
            self.fail_next_update = False
            raise DiscordGatewayError("Discord refused to update the thread")

        thread = self._wake(thread_id)
        self.updates.append(thread_id)

        wanted = truncate_thread_name(name)
        if thread.name != wanted:
            thread.name = wanted
            self.renames.append((thread_id, wanted))

        if message_id is None or message_id not in thread.messages:
            message_id = self._allocate()
        thread.messages[message_id] = content
        thread.metadata_message_id = message_id
        return ThreadHandle(thread_id=thread_id, message_id=message_id)

    async def set_locked(self, *, thread_id: int, locked: bool) -> None:
        self.lock_calls.append((thread_id, locked))
        if self.refuses_every_lock:
            raise DiscordPermissionError("Discord will not let the bot lock the thread")
        if self.fail_next_lock:
            self.fail_next_lock = False
            raise DiscordGatewayError("Discord refused to lock the thread")
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        if thread.locked == locked and not thread.archived:
            return
        # The real gateway unarchives in the same edit that changes the lock.
        thread.archived = False
        if thread.locked != locked:
            thread.locked = locked
            self.locks.append((thread_id, locked))

    async def post(self, *, thread_id: int, content: str) -> int | None:
        thread = self._wake(thread_id)
        message_id = self._allocate()
        thread.messages[message_id] = content
        self.posts.append((thread_id, content))
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
