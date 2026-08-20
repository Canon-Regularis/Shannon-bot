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
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

ROLE = 777000


def asked_of(*teams: str, people: list[dict] | None = None):
    payload = payloads.pull_request_event(
        "review_requested", requested_reviewers=people if people is not None else []
    )
    payload["pull_request"]["requested_teams"] = [{"slug": slug} for slug in teams]
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
            guild_id=1, github_usernames=["backend-team"]
        )
        assert found == {"backend-team": ROLE}

    async def test_pointing_a_team_somewhere_new_replaces_the_old_role(
        self, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
    ) -> None:
        service = TeamLinkingService(db_sessionmaker)
        await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE)

        await service.link(guild_id=1, github_team="backend", discord_role_id=ROLE + 1)

        found = await TeamLinkStore(db_session).resolve_many(
            guild_id=1, github_usernames=["backend"]
        )
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
            guild_id=1, github_usernames=["backend", "design"]
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

        found = await TeamLinkStore(db_session).resolve_many(
            guild_id=2, github_usernames=["backend"]
        )
        assert found == {}

    async def test_asking_about_nobody_asks_the_database_nothing(
        self, db_session: AsyncSession
    ) -> None:
        assert await TeamLinkStore(db_session).resolve_many(guild_id=1, github_usernames=[]) == {}


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

        await UserLinkingService(db_sessionmaker).link(
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

        await UserLinkingService(db_sessionmaker).link(
            guild_id=1, github_username="security", discord_user_id=424242
        )

        await notifying.sync(asked_of("security"))

        assert "Review requested from security." in posts(threads)
        assert not any("<@424242>" in body for body in posts(threads))
