from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping, ItemAssignment, Repository, TrackedItem, WebhookEvent
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status

pytestmark = pytest.mark.integration


def make_repository(*, guild_id: int = 1, repo_id: int = 100) -> Repository:
    return Repository(
        github_repo_id=repo_id,
        repo_name="Canon-Regularis/Shannon-bot",
        repo_url="https://github.com/Canon-Regularis/Shannon-bot",
        discord_guild_id=guild_id,
    )


def make_tracked_item(repository_id: int, *, object_id: int = 555) -> TrackedItem:
    return TrackedItem(
        repository_id=repository_id,
        github_object_id=object_id,
        github_object_type=ObjectType.PR,
        github_object_number=7,
        github_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        title="Add webhook endpoint",
        github_state="open",
        status=Status.NOT_REVIEWED,
        priority=Priority.UNSET,
    )


async def test_one_repository_per_guild(db_session: AsyncSession) -> None:
    db_session.add(make_repository(guild_id=1, repo_id=100))
    await db_session.commit()

    db_session.add(make_repository(guild_id=1, repo_id=200))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_repository_bound_to_one_guild(db_session: AsyncSession) -> None:
    db_session.add(make_repository(guild_id=1, repo_id=100))
    await db_session.commit()

    db_session.add(make_repository(guild_id=2, repo_id=100))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_duplicate_tracked_item_is_rejected(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id))
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_same_object_id_allowed_for_different_object_types(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id, object_id=555))
    issue = make_tracked_item(repository.id, object_id=555)
    issue.github_object_type = ObjectType.ISSUE
    db_session.add(issue)

    await db_session.commit()


async def test_one_channel_per_object_type(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(
        ChannelMapping(
            repository_id=repository.id, object_type=ObjectType.PR, discord_channel_id=42
        )
    )
    await db_session.commit()

    db_session.add(
        ChannelMapping(
            repository_id=repository.id, object_type=ObjectType.PR, discord_channel_id=43
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_assignment_is_unique_per_user_and_role(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()
    item = make_tracked_item(repository.id)
    db_session.add(item)
    await db_session.commit()

    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.REVIEWER
        )
    )
    await db_session.commit()

    # Same person in a different role is legitimate.
    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.ASSIGNEE
        )
    )
    await db_session.commit()

    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.REVIEWER
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_delivery_id_is_unique(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookEvent(
            github_delivery_id="abc-123",
            event_type="pull_request",
            payload_hash="0" * 64,
            status="PROCESSED",
        )
    )
    await db_session.commit()

    db_session.add(
        WebhookEvent(
            github_delivery_id="abc-123",
            event_type="pull_request",
            payload_hash="1" * 64,
            status="PROCESSED",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_repository_cascades(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()
    item = make_tracked_item(repository.id)
    db_session.add(item)
    await db_session.commit()
    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.AUTHOR
        )
    )
    await db_session.commit()

    await db_session.delete(repository)
    await db_session.commit()
    db_session.expunge_all()

    assert await db_session.get(TrackedItem, item.id) is None


def test_every_table_is_truncated_between_tests() -> None:
    """A table missing from the list leaks rows into the next test.

    That is not a loud failure: it is a test that passes or fails according to what ran before
    it, which is the hardest kind to find. `team_links` was added and left out, and the symptom
    was a team resolving to a role no test in that file had linked.
    """
    from shannon.db.base import Base
    from tests.integration.conftest import TABLES

    assert set(TABLES) == set(Base.metadata.tables), (
        "the truncation list and the schema have drifted apart"
    )


class TestLettingGoOfARepository:
    """What the database takes with a repository, and what it deliberately keeps.

    Nothing deletes one yet. These pin the cascades anyway, because the command that will is only
    safe if they hold: it does one DELETE and trusts the schema for the rest, and a cascade that
    quietly stopped cascading would leave rows pointing at a repository that is gone.

    This tests the DATABASE rather than the session, which is what makes it worth writing. The
    relationships carry `passive_deletes=True`, so SQLAlchemy does not load the children and
    delete them itself; it leaves them to PostgreSQL. Without that flag the same assertions would
    pass with no `ondelete` anywhere and prove nothing at all.
    """

    async def test_deleting_a_repository_takes_its_items_with_it(
        self, db_session: AsyncSession
    ) -> None:
        repository = make_repository()
        db_session.add(repository)
        await db_session.commit()
        db_session.add(
            ChannelMapping(
                repository_id=repository.id,
                object_type=ObjectType.PR,
                discord_channel_id=99,
            )
        )
        item = make_tracked_item(repository.id)
        db_session.add(item)
        await db_session.commit()
        db_session.add(
            ItemAssignment(
                tracked_item_id=item.id,
                github_username="octocat",
                role_type=ActorRole.AUTHOR,
            )
        )
        await db_session.commit()

        await db_session.delete(repository)
        await db_session.commit()

        assert await db_session.scalar(select(func.count()).select_from(ChannelMapping)) == 0
        assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 0
        assert await db_session.scalar(select(func.count()).select_from(ItemAssignment)) == 0

    async def test_it_leaves_what_the_guild_decided_about_itself_alone(
        self, db_session: AsyncSession
    ) -> None:
        """These are facts about the server, not about which repository it happens to mirror.
        None carries a foreign key here, so surviving is what the schema already does; this is
        what stops somebody adding a delete for them later.

        `muted_members` is the one that would be worst to get wrong. A row there is somebody
        saying they do not want this bot to ring them, so deleting it on a re-register would
        quietly start notifying a person who had asked to be left alone, and nothing anywhere
        would say so. An account link coming back is an inconvenience; that is a consent record.
        """
        repository = make_repository()
        db_session.add(repository)
        await db_session.commit()
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="octocat", github_user_id=1, discord_user_id=555
        )
        await TeamLinkStore(db_session).link(guild_id=1, github_team="backend", discord_role_id=777)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=555)
        await db_session.commit()

        await db_session.delete(repository)
        await db_session.commit()

        assert await UserLinkStore(db_session).resolve_many(guild_id=1, people={"octocat": 1}) == {
            "octocat": 555
        }
        assert await TeamLinkStore(db_session).resolve_many(
            guild_id=1, people={"backend": None}
        ) == {"backend": 777}
        assert await MutedMemberStore(db_session).is_muted(guild_id=1, discord_user_id=555) is True
