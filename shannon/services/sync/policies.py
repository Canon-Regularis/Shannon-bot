from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from shannon.discord_bot.formatting import format_issue, format_pull_request
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.models import Actor, IssueSnapshot, PullRequestSnapshot, TrackedSnapshot


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
        return format_pull_request(snapshot, status=status, priority=priority, mentions=mentions)

    def assignments(self, snapshot: PullRequestSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
            ActorRole.REVIEWER: snapshot.reviewers,
        }

    def status_for(self, snapshot: PullRequestSnapshot, current: Status) -> Status:
        """Closing a pull request does not move its workflow status; MVP 3 owns that."""
        return current

    def locked(self, snapshot: PullRequestSnapshot) -> bool | None:
        """Pull request threads are never locked automatically; MVP 3 owns that."""
        return None


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
        return format_issue(snapshot, status=status, priority=priority, mentions=mentions)

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
