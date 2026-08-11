from __future__ import annotations

from dataclasses import dataclass, field

from shannon.discord_bot.errors import ThreadNotFoundError
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
        self.renames: list[tuple[int, str]] = []
        self.locks: list[tuple[int, bool]] = []
        self._next_id = 1000

    def _allocate(self) -> int:
        self._next_id += 1
        return self._next_id

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        thread_id = self._allocate()
        message_id = self._allocate()
        thread = FakeThread(
            thread_id=thread_id,
            channel_id=channel_id,
            name=truncate_thread_name(name),
            messages={message_id: content},
            metadata_message_id=message_id,
        )
        self.threads[thread_id] = thread
        self.created.append(thread)
        return ThreadHandle(thread_id=thread_id, message_id=message_id)

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle:
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        # Locked is fine: the bot holds Manage Threads. Archived is not, because Discord
        # rejects writes to an archived thread until it is unarchived.
        if thread.archived:
            raise ThreadNotFoundError(f"Thread {thread_id} is archived")

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
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        if thread.locked == locked:
            return
        thread.locked = locked
        self.locks.append((thread_id, locked))

    async def post(self, *, thread_id: int, content: str) -> int | None:
        thread = self.threads.get(thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread {thread_id} is not reachable")
        # Locked is fine: the bot holds Manage Threads. Archived is not, because Discord
        # rejects writes to an archived thread until it is unarchived.
        if thread.archived:
            raise ThreadNotFoundError(f"Thread {thread_id} is archived")
        message_id = self._allocate()
        thread.messages[message_id] = content
        self.posts.append((thread_id, content))
        return message_id

    def metadata_of(self, thread_id: int) -> str:
        thread = self.threads[thread_id]
        assert thread.metadata_message_id is not None
        return thread.messages[thread.metadata_message_id]
