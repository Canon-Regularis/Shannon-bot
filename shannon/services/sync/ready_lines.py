"""Telling the people a pull request is waiting on that it has stopped being a draft.

Issue #132. A draft rings nobody, deliberately and in two places: the CI announcer refuses to
notify on one because reviewers have not been asked to look yet, and the card is painted grey
rather than green to say the same thing quietly. Nothing was watching for the moment that stops
being true, so the ask that a draft defers never arrived at all.

Not a notifier. `item_assignments.notified_at` answers once for the life of a row and a reviewer
asked while the pull request was still a draft has already spent theirs, so reading the ledger
here would tell nobody. The claim in `mirrored_notes` is what makes this say a thing once, the
same bargain the CI announcer struck for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.models import Actor, PullRequestSnapshot
from shannon.github import mapping
from shannon.services.audience import reachable
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut
from shannon.services.sync.staleness import is_superseded

logger = logging.getLogger(__name__)

# The half of the draft switch worth a message. `converted_to_draft` is supported too, so the
# card can be repainted, and says nothing out loud: going back into draft asks nobody for
# anything, and a line announcing it would ring the very people it was withdrawing the ask from.
READY_FOR_REVIEW = "ready_for_review"


class Renderer(Protocol):
    """The words, which are the one thing this does not decide.

    A protocol rather than a `Callable` alias, because everything but the initiator is keyword
    only and a `Callable` cannot say so. The disagreement would show up nowhere until a TypeError
    inside a Discord phase failed the delivery, on every retry.
    """

    def __call__(
        self,
        initiator: Actor | None,
        *,
        people: Sequence[Actor],
        teams: Sequence[Actor],
        mentions: Mapping[str, int] | None,
        roles: Mapping[str, int] | None,
    ) -> Panel: ...


class ReadyLine:
    """Posts one line naming who marked a pull request ready, and rings whoever it now waits on."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        render: Renderer,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._line = ClaimedLine(sessionmaker, threads, shut_again)
        self._render = render

    async def say(self, arrival: Arrival) -> None:
        """Announce the ask, unless the pull request has gone back into draft since.

        The guard is what separates this from the tag line, which posts on a superseded delivery
        on purpose. A tag line reports something that happened and stays true however late it is
        read; this one says "go and look at this now", which is false the moment the pull request
        is a draft again, and it says it by ringing people.
        """
        snapshot = _made_ready(arrival)
        if snapshot is None:
            return

        async with self._sessionmaker() as session:
            found = await TrackedItemStore(session).get_with_its_server(arrival.tracked_item_id)
            # The row cannot be missing here, since the delivery only reached a thread by way of
            # it. Checked anyway, because the cost of being wrong is an attribute read on None
            # inside a Discord call.
            if found is None:
                logger.info(
                    "tracked item %s has gone, so nothing is said about it leaving draft",
                    arrival.tracked_item_id,
                )
                return
            item, guild_id = found

            if is_superseded(
                snapshot.updated_at,
                item.github_updated_at,
                arrived=arrival.arrived,
                applied=item.last_delivery_id,
            ):
                logger.info(
                    "tracked item %s has moved on since it was marked ready, so nobody is rung",
                    arrival.tracked_item_id,
                )
                return

            initiator = mapping.actor(arrival.payload.get("sender"))
            people, teams = _who_to_tell(snapshot, initiator)
            audience = await reachable(session, guild_id=guild_id, people=people, teams=teams)

        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            # Keyed on the delivery, like every other announcer. A pull request pushed back into
            # draft and marked ready again is a second delivery and says it a second time, which
            # is right: the ask was withdrawn and made again.
            note_key=f"ready:{arrival.arrived}",
            panel=self._render(
                initiator,
                people=people,
                teams=teams,
                mentions=audience.mentions,
                roles=audience.roles,
            ),
            notify=audience.notify,
        )
        logger.info(
            "%s#%s has left draft, telling %s and %s",
            snapshot.repository.full_name,
            snapshot.number,
            [person.login for person in people],
            [team.login for team in teams],
        )


def _made_ready(arrival: Arrival) -> PullRequestSnapshot | None:
    """This delivery's pull request, where this delivery is one leaving draft.

    The type check is not ceremony. One tuple of announcers serves the issues handler as well as
    the pull request one, so this is handed every issue delivery too, and an issue has no
    reviewers to tell and no draft to leave.
    """
    if arrival.action != READY_FOR_REVIEW:
        return None
    if not isinstance(arrival.snapshot, PullRequestSnapshot):
        return None
    return arrival.snapshot


def _who_to_tell(
    snapshot: PullRequestSnapshot, initiator: Actor | None
) -> tuple[tuple[Actor, ...], tuple[Actor, ...]]:
    """Who is waiting on this pull request now that it is not a draft.

    Reviewers and assignees together and deduped by login, because GitHub keeps the two lists
    apart and somebody on both would otherwise be named twice in one sentence and rung twice for
    one event.

    The person who pressed the button is dropped, rather than the author. The two are usually the
    same and the rule reads the same either way, but where a maintainer marks somebody else's
    pull request ready the author is exactly who wants telling and the initiator is exactly who
    does not.

    Teams are kept whatever happens. GitHub does not say who is in one, so a role mention cannot
    leave the initiator out, and `/mentions` cannot either.
    """
    pressed = initiator.login.lower() if initiator is not None else None
    people = {
        person.login.lower(): person
        for person in (*snapshot.reviewers, *snapshot.assignees)
        if person.login.lower() != pressed
    }
    return tuple(people.values()), tuple(snapshot.reviewer_teams)
