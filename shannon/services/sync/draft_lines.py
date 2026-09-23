"""Saying out loud that a pull request has crossed into or out of draft.

Issues #132, #139 and #140. A draft rings nobody, deliberately and in two places: the CI
announcer refuses to notify on one because reviewers have not been asked to look yet, and the
card is painted grey rather than green to say the same thing quietly. Nothing was watching for
either moment that changes, so the ask a draft defers never arrived, and its withdrawal never
did either.

One class, built twice. The two halves differ in four values — which action they answer, which
key they claim, which words they post, and whether a team becomes a role mention — and in
nothing else. A branch inside one instance would sit between which action arrived and which
words go under which key, which is the one mistake here that is both silent and permanent: a
sentence posted under the other half's key is claimed, and never said again.

Not a notifier. `item_assignments.notified_at` answers once for the life of a row and a reviewer
asked while the pull request was still a draft has already spent theirs, so reading the ledger
here would tell nobody. The claim in `mirrored_notes` is what makes this say a thing once, the
same bargain the CI announcer struck for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class Half:
    """One side of the draft switch, and the three things that make it that side.

    `rings_roles` is the only one that is not a label. A linked GitHub team becomes a role
    mention on the way in and its plain name on the way out, because a role rings everybody
    holding it and Discord gives nobody a way to leave one person out of one: asking a team to
    look is worth that, and telling them to stop is not worth waking them for.
    """

    action: str
    note: str
    rings_roles: bool


READY = Half(action="ready_for_review", note="ready", rings_roles=True)
DRAFTED = Half(action="converted_to_draft", note="draft", rings_roles=False)


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


class DraftSwitchLine:
    """Posts one line naming who threw the switch, and tells whoever the pull request concerns."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        half: Half,
        render: Renderer,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._line = ClaimedLine(sessionmaker, threads, shut_again)
        self._half = half
        self._render = render

    async def say(self, arrival: Arrival) -> None:
        """Announce the switch, unless the pull request has been thrown back since.

        The guard is what separates this from the tag line, which posts on a superseded delivery
        on purpose. A tag line reports something that happened and stays true however late it is
        read. Both of these go stale, in the same way and for the same reason: one says "go and
        look at this now" and the other says "stop looking", and each is false the moment the
        other half is thrown. Each says it by ringing people, which is why being late matters
        here and does not there.
        """
        snapshot = _thrown(arrival, self._half)
        if snapshot is None:
            return

        async with self._sessionmaker() as session:
            found = await TrackedItemStore(session).get_with_its_server(arrival.tracked_item_id)
            # The row cannot be missing here, since the delivery only reached a thread by way of
            # it. Checked anyway, because the cost of being wrong is an attribute read on None
            # inside a Discord call.
            if found is None:
                logger.info(
                    "tracked item %s has gone, so nothing is said about its %s",
                    arrival.tracked_item_id,
                    self._half.action,
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
                    "tracked item %s has moved on since its %s, so nobody is rung",
                    arrival.tracked_item_id,
                    self._half.action,
                )
                return

            initiator = mapping.actor(arrival.payload.get("sender"))
            people, teams = _who_to_tell(snapshot, initiator)
            # Handed no teams on the way back into draft, so nothing resolves a slug to a role
            # and `_role` falls through to the plain name. `reachable` answers an empty mapping
            # without asking the database, so this costs a comparison rather than a query.
            audience = await reachable(
                session,
                guild_id=guild_id,
                people=people,
                teams=teams if self._half.rings_roles else (),
            )

        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            # Keyed on the delivery, like every other announcer, and on the half as well so the
            # two cannot claim each other's. A pull request pushed back into draft and marked
            # ready again is three deliveries and says three things, which is right: the ask was
            # made, withdrawn, and made again.
            note_key=f"{self._half.note}:{arrival.arrived}",
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
            "%s#%s: %s, telling %s and %s",
            snapshot.repository.full_name,
            snapshot.number,
            self._half.action,
            [person.login for person in people],
            [team.login for team in teams],
        )


def _thrown(arrival: Arrival, half: Half) -> PullRequestSnapshot | None:
    """This delivery's pull request, where this delivery threw this half of the switch.

    The type check is not ceremony. One tuple of announcers serves the issues handler as well as
    the pull request one, so this is handed every issue delivery too, and an issue has no
    reviewers to tell and no draft to be in either way.
    """
    if arrival.action != half.action:
        return None
    if not isinstance(arrival.snapshot, PullRequestSnapshot):
        return None
    return arrival.snapshot


def _who_to_tell(
    snapshot: PullRequestSnapshot, initiator: Actor | None
) -> tuple[tuple[Actor, ...], tuple[Actor, ...]]:
    """Who this switch concerns, on whichever side of it the delivery is.

    Reviewers, assignees and the author together and deduped by login, because GitHub keeps the
    lists apart and somebody on two of them would otherwise be named twice in one sentence and
    rung twice for one event.

    The author is added by name rather than looked for among the reviewers. GitHub refuses a
    review request from the person who opened a pull request, so the author is never in that list
    however the item was set up, and before issue #139 they were told only where somebody had
    happened to assign them. Last in the order, so an author who IS assigned keeps the place the
    assignee list gave them.

    All of them, minus whoever pressed the button. The initiator is dropped because they know:
    they pressed it. That one rule leaves out a self-marking author without having to mention
    authors at all, and dropping the author instead would get the interesting case backwards,
    since a maintainer marking somebody else's pull request ready — or putting it back into
    draft — is exactly when its author wants telling.

    Teams are kept whatever happens. GitHub does not say who is in one, so no rule here can leave
    the initiator out of a team, and `/mentions` cannot either. Whether a team becomes a role
    mention or its plain name is the caller's, and the two halves answer it differently.
    """
    pressed = initiator.login.lower() if initiator is not None else None
    author = (snapshot.author,) if snapshot.author else ()
    people = {
        person.login.lower(): person
        for person in (*snapshot.reviewers, *snapshot.assignees, *author)
        if person.login.lower() != pressed
    }
    return tuple(people.values()), tuple(snapshot.reviewer_teams)
