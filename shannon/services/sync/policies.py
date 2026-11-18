from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from shannon.discord_bot import formatting
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.rich_text import as_description
from shannon.domain.board import status_from_column
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    TicketSnapshot,
    TrackedSnapshot,
)
from shannon.github.mentions import names_a_note_writes


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

    def names_written(self, snapshot: TrackedSnapshot) -> tuple[str, ...]:
        """The logins the description @names where the block shows it, issue #259.

        Lowercased, in the order they are written, and no more than the ten the swap reaches.
        Teams are left out: the block writes them as text and never as a role mention, so there
        is nobody to look up for one. Read here rather than in the sync because only the kind
        knows whether its block shows a description it should ring for at all.
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

    def names_written(self, snapshot: TrackedSnapshot) -> tuple[str, ...]:
        return _named_in_the_description(snapshot)

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

    def names_written(self, snapshot: TrackedSnapshot) -> tuple[str, ...]:
        return _named_in_the_description(snapshot)

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
        """GitHub decides: an open issue is refused `/status Done`, so only closing it shuts it."""
        return snapshot.closed

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """The answer `shut` gives, read from the column the payload writes into.

        Not the status, which only follows it: `/status Done` is refused for an open issue, and
        the sync that writes the state turns an open issue's DONE back. An open issue's thread is
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
    # and its thread was never locked for it. The one lock a ticket thread does get - its card
    # archived or deleted off the board, issue #198 - is the poller's, set and lifted beside the
    # card's own state rather than through a sync, so nothing here reads or settles it.
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
        # `mentions` goes through since issue #182, because a card names a creator and assignees
        # now. `priority` still does not: a card's is the board's single-select, carried on the
        # snapshot, and the row's label-derived one is `UNSET` for a draft whatever the board says.
        return formatting.format_ticket(snapshot, status=status, mentions=mentions)

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Still nobody, and since issue #182 that is a decision rather than a fact.

        A card does carry a creator and assignees now, and the block shows them. What this method
        feeds is something else: the assignment ledger, and the notifier that pings somebody the
        first time they are put on an item. Answering here would turn that on for every card on a
        board at once, which is the thing `container`'s refresh wiring already guards against -
        "a card that one day carries an assignee cannot start notifying a backlog by inheriting a
        default". Issue #182 asked for a card's metadata to be shown and its changes announced, and
        neither of those goes through here.
        """
        return {}

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        return {}

    def names_written(self, snapshot: TrackedSnapshot) -> tuple[str, ...]:
        """Nobody, issue #259, for the reason `assignments` answers nobody.

        A draft card's body is shown, and its names stay as written there: `format_ticket` swaps
        nothing into it. Answering here would ring for names the block never shows as mentions,
        and would turn it on for every card on a board at once.
        """
        return ()

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status:
        """The board is the source: its column is the status.

        A column nobody has taught us leaves the status where it was; a default would move real
        work backwards every time the board is read.
        """
        assert isinstance(snapshot, TicketSnapshot)
        return status_from_column(snapshot.column) or current

    def shut(self, snapshot: TrackedSnapshot, *, status: Status) -> bool | None:
        """Left alone. A board column is not a closed state.

        Moving a card out of Done is not an event either, so a lock set here would have nothing to
        lift it. What does end a card is taking it off the board, and that is followed by the poller
        rather than here (issue #198): an archived card is never synced at all, so a sync is never
        the place that learns of it.
        """
        assert isinstance(snapshot, TicketSnapshot)
        return None

    def shut_for_state(self, *, status: Status, github_state: str) -> bool:
        """Never: a card in Done is a card somebody can drag back out.

        Not for an archived or deleted card either, whose row says so since issue #198. Their lock
        belongs to the poller, which took it; this is asked only by a sync settling a lock it owes,
        and no sync owes a ticket one.
        """
        return False

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        """No number in front: a draft item has none."""
        return snapshot.title.strip() or "Untitled ticket"


def _named_in_the_description(snapshot: TrackedSnapshot) -> tuple[str, ...]:
    """The people an issue's or a pull request's description names, read off what its block shows.

    `as_description` and `names_a_note_writes` because the block swaps names into that string with
    `rewrite_a_note`, issue #259, and the two have to agree to the character. A name this missed
    would show as a mention and ring nobody; a name read past the preview cut would ring somebody
    the block never shows, and spend the budget of ten the swap shares with it.
    """
    described = as_description(snapshot.body, repository=snapshot.repository)
    return names_a_note_writes(described.text).people


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
