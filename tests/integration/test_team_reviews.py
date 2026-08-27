"""A review asked of a GitHub team, from the payload to the ping.

Teams were the last thing the requirements asked for that this bot did not do. They were read but
not told, because there was nowhere to look up who a team is on Discord's side, and giving them
assignment rows without that would have reintroduced a double ping: the stamp that closes a review
request is set by the login of whoever submitted, which is never a team.

So the two halves arrived together. `team_links` says which role a team is, and a submitted review
closes every open team request on the item, which is deliberately wrong in the safe direction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _both
from shannon.db.models import ItemAssignment, Repository
from shannon.db.stores.team_links import TeamLinkStore
from shannon.discord_bot.formatting import format_reviewer_ping, format_team_ping
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.linking import InvalidGitHubTeamError, TeamLinkingService
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

ROLE = 777000


def asked_of(
    *teams: str,
    people: list[dict] | None = None,
    now: str | None = None,
    at: str | None = None,
):
    """A `review_requested` body.

    `now` is the team GitHub names at the top level, which is the one this event asked for. Left
    out by default so the tests that are not about that keep the payload they had.
    """
    payload = payloads.pull_request_event(
        "review_requested", requested_reviewers=people if people is not None else []
    )
    payload["pull_request"]["requested_teams"] = [{"slug": slug} for slug in teams]
    if at is not None:
        payload["pull_request"]["updated_at"] = at
    if now is not None:
        payload["requested_team"] = {"slug": now}
    snapshot = parse_pull_request_event("review_requested", payload)
    assert snapshot is not None
    return snapshot


@pytest.fixture
def notifying(db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway) -> ItemSyncService:
    """The pull request path with both notifiers, the way the container assembles it."""
    return build_item_sync(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        _both(
            ActorNotifier(
                db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
            ActorNotifier(
                db_sessionmaker,
                threads,
                role=ActorRole.REVIEWER_TEAM,
                render=format_team_ping,
                mentions=TeamLinkStore,
            ),
        ),
    )


async def link_team(session: AsyncSession, slug: str, role_id: int = ROLE) -> None:
    await TeamLinkStore(session).link(guild_id=1, github_team=slug, discord_role_id=role_id)
    await session.commit()


def posts(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts]


def told(threads: FakeThreadGateway) -> int:
    return sum("Review requested from" in body for body in posts(threads))


class TestTellingATeam:
    async def test_a_linked_team_is_pinged_as_a_role(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        await link_team(db_session, "backend")

        await notifying.sync(asked_of("backend"))

        assert f"Review requested from <@&{ROLE}>." in posts(threads)

    async def test_a_team_nobody_has_linked_is_still_named(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        """The bargain the people renderer already makes: the thread records who GitHub asked
        for, even where the server has never run /link_team."""
        await notifying.sync(asked_of("backend"))

        assert "Review requested from backend." in posts(threads)

    async def test_people_and_teams_are_told_separately(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """Different words off different tables, so two messages rather than one."""
        await link_team(db_session, "backend")

        await notifying.sync(asked_of("backend", people=[payloads.user("monalisa", 3)]))

        assert "Review requested from monalisa." in posts(threads)
        assert f"Review requested from <@&{ROLE}>." in posts(threads)

    async def test_a_team_is_told_once(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        await notifying.sync(asked_of("backend"))

        await notifying.sync(asked_of("backend"))

        assert told(threads) == 1


class TestClosingATeamsRequest:
    async def test_a_review_leaves_the_team_rows_alone(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
    ) -> None:
        """A team's request is closed by GitHub dropping it, which deletes the row on the next
        delivery. Stamping it here instead made it look like an answered request, and the next
        ordinary event reopened it and pinged the role for something nobody had asked twice.
        """
        await notifying.sync(asked_of("backend", "design"))
        review = parse_review_event("submitted", payloads.pull_request_review_event("submitted"))

        await ReviewRequestLedger(db_sessionmaker).fulfilled(review)

        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER_TEAM)
            )
        ).all()
        assert len(rows) == 2
        assert all(row.fulfilled_at is None for row in rows), "a team request was closed early"

    async def test_a_team_that_github_drops_loses_its_row(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_session: AsyncSession,
    ) -> None:
        """Which is the whole mechanism a team needs: no row, no claim, and a fresh row with a
        fresh ping if the team is ever asked again."""
        await notifying.sync(asked_of("backend", "design"))

        await notifying.sync(asked_of("backend"))

        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER_TEAM)
            )
        ).all()
        assert [row.github_username for row in rows] == ["backend"]

    async def test_a_replayed_request_does_not_ping_the_team_again(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
    ) -> None:
        """The case the stamp exists for, which a team could not reach before it had one: a
        delivery captured before the review and retried after it still lists the team."""
        await notifying.sync(asked_of("backend"))
        review = parse_review_event("submitted", payloads.pull_request_review_event("submitted"))
        await ReviewRequestLedger(db_sessionmaker).fulfilled(review)
        before = told(threads)

        await notifying.sync(asked_of("backend"))

        assert told(threads) == before


class TestLinkingATeam:
    async def test_a_slug_is_stored_lowercased(
        self, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
    ) -> None:
        """GitHub lowercases a slug itself, so matching case sensitively would only make a
        hand-typed name fail to find the row it just wrote."""
        await TeamLinkingService(db_sessionmaker).link(
            guild_id=1, github_team="@Backend-Team", discord_role_id=ROLE
        )

        found = await TeamLinkStore(db_session).resolve_many(
            guild_id=1, people={"backend-team": None}
        )
        assert found == {"backend-team": ROLE}

    async def test_pointing_a_team_somewhere_new_replaces_the_old_role(
        self, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
    ) -> None:
        service = TeamLinkingService(db_sessionmaker)
        await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE)

        await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE + 1)

        found = await TeamLinkStore(db_session).resolve_many(guild_id=1, people={"backend": None})
        assert found == {"backend": ROLE + 1}

    async def test_two_teams_may_share_one_role(
        self, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
    ) -> None:
        """Unlike a person, whose account belongs to them. A server may keep one reviewers role
        that several teams should reach."""
        service = TeamLinkingService(db_sessionmaker)
        await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE)
        await service.link(guild_id=1, github_team="design", discord_role_id=ROLE)

        found = await TeamLinkStore(db_session).resolve_many(
            guild_id=1, people={"backend": None, "design": None}
        )
        assert found == {"backend": ROLE, "design": ROLE}

    @pytest.mark.parametrize("slug", ["", "  ", "-leading", "a" * 200, "not a team"])
    async def test_something_that_is_not_a_team_is_refused(
        self, db_sessionmaker: async_sessionmaker, slug: str
    ) -> None:
        with pytest.raises(InvalidGitHubTeamError):
            await TeamLinkingService(db_sessionmaker).link(
                guild_id=1, github_team=slug, discord_role_id=ROLE
            )

    async def test_a_guild_only_sees_its_own_links(
        self, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
    ) -> None:
        await TeamLinkingService(db_sessionmaker).link(
            guild_id=1, github_team="backend", discord_role_id=ROLE
        )

        found = await TeamLinkStore(db_session).resolve_many(guild_id=2, people={"backend": None})
        assert found == {}

    async def test_asking_about_nobody_asks_the_database_nothing(
        self, db_session: AsyncSession
    ) -> None:
        assert await TeamLinkStore(db_session).resolve_many(guild_id=1, people={}) == {}


class TestATeamIsNotToldTwice:
    """The failure a review found: closing a request nobody answered makes it reopenable."""

    async def test_an_unrelated_event_after_a_review_does_not_ping_the_team_again(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
    ) -> None:
        """A review by one person closed every team's request, including teams GitHub never
        dismissed. The stamp then made those rows look like answered requests, so the next
        ordinary event with a later timestamp reopened them and pinged the role again, once per
        review round, for a request that was never answered and never re-made.
        """
        await notifying.sync(asked_of("backend", "design"))
        review = parse_review_event("submitted", payloads.pull_request_review_event("submitted"))
        await ReviewRequestLedger(db_sessionmaker).fulfilled(review)
        before = told(threads)

        # Any handled action with a newer timestamp. Labelling is the most ordinary there is.
        later = payloads.pull_request_event(
            "labeled", updated_at="2026-08-12T09:00:00Z", requested_reviewers=[]
        )
        later["pull_request"]["requested_teams"] = [{"slug": "backend"}, {"slug": "design"}]
        moved = parse_pull_request_event("labeled", later)
        assert moved is not None
        await notifying.sync(moved)

        assert told(threads) == before, "an unrelated event re-pinged a team"


class TestAskingATeamAgain:
    """The gap left by closing a team's request only when GitHub drops it from the list.

    GitHub drops a team the moment any member submits a review, and sends no `pull_request`
    event saying so. Nothing here deletes the row, so the next ask of that team arrives with
    the list exactly as it was, `replace` leaves the row alone with its ping still stamped, and
    nobody is told. There is no escape from it either: `synchronize` is not handled, so a round
    of review, fixes and re-request produces no delivery that would have deleted the row.

    What separates the second ask from the first is the team GitHub names at the top level of a
    `review_requested` event, which it only sends for a party that was not already requested.
    """

    async def test_a_team_asked_again_is_told_again(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        await notifying.sync(asked_of("backend", now="backend", at="2026-08-10T12:00:00Z"))

        await notifying.sync(asked_of("backend", now="backend", at="2026-08-12T09:00:00Z"))

        assert told(threads) == 2, "the second ask of a team told nobody"

    async def test_the_same_delivery_arriving_twice_tells_them_once(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        """Deliveries are at least once, so the payload that asked is also the payload a retry
        replays. It says the same thing both times, and only the timestamp says which is which.
        """
        asked = asked_of("backend", now="backend", at="2026-08-10T12:00:00Z")
        await notifying.sync(asked)

        await notifying.sync(asked)

        assert told(threads) == 1, "a replayed delivery pinged the team a second time"

    async def test_a_team_nobody_asked_again_is_left_alone(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        """One event asks for one party. The other teams on the pull request are still waiting
        on the request they were already given, and clearing their stamp would ping them for
        somebody else's ask.
        """
        await notifying.sync(
            asked_of("backend", "design", now="backend", at="2026-08-10T12:00:00Z")
        )
        before = told(threads)

        await notifying.sync(asked_of("backend", "design", now="design", at="2026-08-12T09:00:00Z"))

        assert told(threads) == before + 1, "the ask for one team was spread across both"

    async def test_a_person_asked_again_with_no_review_to_show_for_it_is_told_again(
        self, registered: Repository, notifying: ItemSyncService, threads: FakeThreadGateway
    ) -> None:
        """A person's request is normally closed by the review event that answers it, and a
        later payload measured against that stamp reopens it. When that event never arrives, a
        person has the same hole a team does, and closes it the same way.
        """
        monalisa = [payloads.user("monalisa", 3)]
        await notifying.sync(asked_of(people=monalisa, at="2026-08-10T12:00:00Z"))

        await notifying.sync(asked_of(people=monalisa, at="2026-08-12T09:00:00Z"))
        told_without_the_top_level_name = told(threads)

        payload = payloads.pull_request_event(
            "review_requested", requested_reviewers=monalisa, updated_at="2026-08-13T09:00:00Z"
        )
        payload["requested_reviewer"] = payloads.user("monalisa", 3)
        again = parse_pull_request_event("review_requested", payload)
        assert again is not None
        await notifying.sync(again)

        assert told_without_the_top_level_name == 1, "an ordinary event re-pinged a reviewer"
        assert told(threads) == 2


class TestATeamIsNotAPerson:
    """A slug and a login are separate namespaces on GitHub, and one of them is claimable here.

    `/link` binds a GitHub name to a Discord account with no gate when somebody claims it for
    themselves, and GitHub is never asked whether the name is theirs. So a member can link
    `security` to themselves, and if a team slug were ever looked up in the account map they
    would appear as, and be pinged as, the `security` team on every pull request in the server.
    """

    async def test_a_team_is_never_rendered_as_a_linked_person(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
    ) -> None:
        from shannon.services.linking import UserLinkingService

        await UserLinkingService(db_sessionmaker, FakeGitHubClient()).link(
            guild_id=1, github_username="security", discord_user_id=424242
        )

        await notifying.sync(asked_of("security"))

        thread = threads.created[0]
        block = thread.messages[thread.metadata_message_id]
        assert "**Reviewers:** security" in block
        assert "<@424242>" not in block, "a team was rendered as somebody who claimed its name"

    async def test_a_team_is_never_pinged_as_a_linked_person(
        self,
        registered: Repository,
        notifying: ItemSyncService,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
    ) -> None:
        """The ping reads the team map, which only /link_team writes, and that one is gated."""
        from shannon.services.linking import UserLinkingService

        await UserLinkingService(db_sessionmaker, FakeGitHubClient()).link(
            guild_id=1, github_username="security", discord_user_id=424242
        )

        await notifying.sync(asked_of("security"))

        assert "Review requested from security." in posts(threads)
        assert not any("<@424242>" in body for body in posts(threads))


async def test_re_pointing_a_team_moves_its_timestamp(
    db_sessionmaker: async_sessionmaker, db_session: AsyncSession
) -> None:
    """An upsert does not fire SQLAlchemy's `onupdate`, so a row re-pointed at a new role kept
    the timestamp of the first link and read as untouched since."""
    from shannon.db.models import TeamLink

    service = TeamLinkingService(db_sessionmaker)
    await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE)
    db_session.expire_all()
    first = (await db_session.scalars(select(TeamLink))).one().updated_at

    await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE + 1)

    db_session.expire_all()
    row = (await db_session.scalars(select(TeamLink))).one()
    assert row.discord_role_id == ROLE + 1
    assert row.updated_at > first, "the row was changed and still reads as untouched"
