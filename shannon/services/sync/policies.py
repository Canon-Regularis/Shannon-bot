from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from shannon.discord_bot import formatting
from shannon.discord_bot.panels import Panel
from shannon.domain.board import status_from_column
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    TicketSnapshot,
    TrackedSnapshot,
)


class SyncPolicy(Protocol):
    """Everything that differs between the kinds of GitHub object being mirrored.

    The sync service holds the orchestration that is the same for all of them.
    """

    object_type: ObjectType

    # Where this kind's threads go when nobody has mapped a channel for it. /register only maps
    # pull requests, so without a fallback an issue has nowhere to go until somebody runs
    # /set_channel. None means no fallback: if it is not mapped, it is not posted.
    channel_fallback: ObjectType | None

    # Whether this kind's DONE means somebody shut the thread. True where a command took the lock
    # and wrote it to the row, the only place a replacement thread can learn it. False where DONE
    # comes from outside Discord, since shutting a replacement would invent a lock.
    lock_lives_in_the_row: bool

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> Panel: ...

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]: ...

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Who this event has just asked for, as opposed to who is on the item.

        Only an event can tell a request made again from a request never withdrawn.
        """
        ...

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status: ...

    def shut(self, snapshot: TrackedSnapshot, *, status: Status) -> bool | None:
        """Whether the thread should be shut, or None to leave it as it is.

        `status` is the row's, as this delivery leaves it. Only the pull request reads it.
        """
        ...

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """Whether an item in this state belongs in a thread that is shut, from the row alone.

        Asked where there is no payload: a delivery turned away as superseded is refused before
        anything reads its snapshot. Unlike `shut` it must answer yes or no.
        """
        ...

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        """What the item's Discord thread is called."""
        ...


class PullRequestPolicy:
    object_type = ObjectType.PR
    channel_fallback: ObjectType | None = None
    # `/status Done` is the only thing that locks one, and no payload can say a pull request is
    # finished, so the row is all a replacement thread has.
    lock_lives_in_the_row = True

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> Panel:
        assert isinstance(snapshot, PullRequestSnapshot)
        return formatting.format_pull_request(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        assert isinstance(snapshot, PullRequestSnapshot)
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
            ActorRole.REVIEWER: snapshot.reviewers,
            ActorRole.REVIEWER_TEAM: snapshot.reviewer_teams,
        }

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Whoever `review_requested` named at the top level; empty for every other action."""
        assert isinstance(snapshot, PullRequestSnapshot)
        return {
            ActorRole.REVIEWER: [snapshot.person_asked_now] if snapshot.person_asked_now else [],
            ActorRole.REVIEWER_TEAM: ([snapshot.team_asked_now] if snapshot.team_asked_now else []),
        }

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status:
        """Closing a pull request does not move its workflow status; MVP 3 owns that."""
        return current

    def shut(self, snapshot: TrackedSnapshot, *, status: Status) -> bool | None:
        """Closed covers merged and abandoned alike.

        An open one at DONE is `/status Done`, which no payload knows about; False elsewhere,
        rather than None, is the only thing that gives a reopened pull request its thread back.
        """
        if snapshot.closed:
            return True
        if status is Status.DONE:
            return None
        return False

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """`/status Done` writes the status, and closing or merging writes the state."""
        return status is Status.DONE or github_state != "open"

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        return formatting.thread_name(snapshot)


class IssuePolicy:
    object_type = ObjectType.ISSUE
    channel_fallback: ObjectType | None = ObjectType.PR
    # `shut` reads it off the payload, so a replacement has nothing to learn from the row.
    lock_lives_in_the_row = False

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> Panel:
        assert isinstance(snapshot, IssueSnapshot)
        return formatting.format_issue(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Issues have no reviewers, so that role is never written for them."""
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
        }

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nothing. Only reviewers can be asked again."""
        return {}

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status:
        """A closed issue is done, and reopening one resets only DONE.

        Forcing NOT_REVIEWED on every open issue would overwrite MVP 3's status commands on the
        next webhook.
        """
        if snapshot.closed:
            return Status.DONE
        if current is Status.DONE:
            return Status.NOT_REVIEWED
        return current

    def shut(self, snapshot: TrackedSnapshot, *, status: Status) -> bool | None:
        """GitHub decides: an issue has no `/status Done`, and the command closes it on GitHub."""
        return snapshot.closed

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """The answer `shut` gives, read from the column the payload writes into.

        Not the status: `/status Done` can put an open issue at DONE, and an open issue's thread is
        one people are still meant to be talking in.
        """
        return github_state == "closed"

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        return formatting.thread_name(snapshot)


class TicketPolicy:
    """A draft item on a project board: a name and a column, and no more.

    No channel fallback, unlike issues: mirroring a board is deliberate.
    """

    object_type = ObjectType.TICKET
    channel_fallback: ObjectType | None = None
    # A card in the Done column is DONE on the row, put there by the board and not by anybody,
    # and its thread was never locked.
    lock_lives_in_the_row = False

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> Panel:
        assert isinstance(snapshot, TicketSnapshot)
        return formatting.format_ticket(snapshot, status=status)

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nobody. A draft item carries no author, assignee or reviewer to record or to ping."""
        return {}

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        return {}

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status:
        """The board is the source: its column is the status.

        A column nobody has taught us leaves the status where it was; a default would move real
        work backwards every time the board is read.
        """
        assert isinstance(snapshot, TicketSnapshot)
        return status_from_column(snapshot.column) or current

    def shut(self, snapshot: TrackedSnapshot, *, status: Status) -> bool | None:
        """Left alone. A board column is not a closed state.

        No GitHub event arrives when a card moves back out of Done, so nothing would open the
        thread again.
        """
        assert isinstance(snapshot, TicketSnapshot)
        return None

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """Never: a card in Done is a card somebody can drag back out."""
        return False

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        """No number in front: a draft item has none."""
        return snapshot.title.strip() or "Untitled ticket"


def channel_fallbacks() -> dict[ObjectType, ObjectType]:
    """Which kinds fall back to another kind's channel, read off the policies themselves.

    `/set_channel` needs it to say where threads already open went: on a server that has never
    mapped issues, that is the pull request channel and not nowhere.
    """
    return {
        policy.object_type: policy.channel_fallback
        for policy in (PullRequestPolicy(), IssuePolicy(), TicketPolicy())
        if policy.channel_fallback is not None
    }
