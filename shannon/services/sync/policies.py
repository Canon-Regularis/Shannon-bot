from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from shannon.discord_bot import formatting
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

    The sync service holds the orchestration that is the same for all of them; a policy holds
    the handful of decisions that are not. Adding a third kind means adding a policy, not
    another copy of the orchestration.
    """

    object_type: ObjectType

    # Where this kind of item's threads go when nobody has mapped a channel for it. /register
    # only ever maps pull requests, so without a fallback an issue has nowhere to go until
    # somebody runs /set_channel, and nothing appears to be happening. None means no fallback:
    # if it is not mapped, it is not posted.
    channel_fallback: ObjectType | None

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str: ...

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]: ...

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status: ...

    def locked(self, snapshot: TrackedSnapshot) -> bool | None:
        """Whether the thread should be locked, or None to leave it as it is."""
        ...

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        """What the item's Discord thread is called.

        Here rather than in the renderer because it is the one piece of the thread that is not
        the metadata block, and because a ticket has no number to lead with while the other two
        are found in a channel list by theirs.
        """
        ...


class PullRequestPolicy:
    object_type = ObjectType.PR
    channel_fallback = None

    def render(
        self,
        snapshot: PullRequestSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_pull_request(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: PullRequestSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
            ActorRole.REVIEWER: snapshot.reviewers,
            ActorRole.REVIEWER_TEAM: snapshot.reviewer_teams,
        }

    def status_for(self, snapshot: PullRequestSnapshot, current: Status) -> Status:
        """Closing a pull request does not move its workflow status; MVP 3 owns that."""
        return current

    def locked(self, snapshot: PullRequestSnapshot) -> bool | None:
        """Pull request threads are never locked automatically; MVP 3 owns that."""
        return None

    def thread_name(self, snapshot: PullRequestSnapshot) -> str:
        return formatting.thread_name(snapshot)


class IssuePolicy:
    object_type = ObjectType.ISSUE
    channel_fallback = ObjectType.PR

    def render(
        self,
        snapshot: IssueSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_issue(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: IssueSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Issues have no reviewers, so that role is never written for them."""
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
        }

    def status_for(self, snapshot: IssueSnapshot, current: Status) -> Status:
        """A closed issue is done, and reopening one undoes that.

        Reopening only resets a status of DONE rather than forcing NOT_REVIEWED on every open
        issue, so that MVP 3's status commands are not overwritten on the next webhook.
        """
        if snapshot.closed:
            return Status.DONE
        if current is Status.DONE:
            return Status.NOT_REVIEWED
        return current

    def locked(self, snapshot: IssueSnapshot) -> bool | None:
        return snapshot.closed

    def thread_name(self, snapshot: IssueSnapshot) -> str:
        return formatting.thread_name(snapshot)


class TicketPolicy:
    """A draft item on a project board, which is a thing with a name and a column and no more.

    No channel fallback, unlike issues. An issue with nowhere to go is a mistake, because
    /register maps pull requests and forgetting /set_channel is easy; a board is something
    somebody chose to mirror, and putting draft items into the pull request channel uninvited
    would be a surprise rather than a kindness.
    """

    object_type = ObjectType.TICKET
    channel_fallback = None

    def render(
        self,
        snapshot: TicketSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_ticket(snapshot, status=status)

    def assignments(self, snapshot: TicketSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nobody. A draft item carries no author, assignee or reviewer to record or to ping."""
        return {}

    def status_for(self, snapshot: TicketSnapshot, current: Status) -> Status:
        """The board is the source: its column is the status, and moving the card is the change.

        A column nobody has taught us leaves the status where it was. Falling back to a default
        would move real work backwards every time the board is read.
        """
        return status_from_column(snapshot.column) or current

    def locked(self, snapshot: TicketSnapshot) -> bool | None:
        """Left alone. A board column is not a closed state, and a ticket that moves back out of
        Done would be locked in a thread nobody could answer in."""
        return None

    def thread_name(self, snapshot: TicketSnapshot) -> str:
        """No number in front. A draft item has none, and the board is where it is found."""
        return snapshot.title.strip() or "Untitled ticket"
