from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment
from shannon.domain.enums import ActorRole
from shannon.domain.models import Actor


class ItemAssignmentStore:
    """Data access for who is attached to a tracked item and in what capacity.

    GitHub logins are stored lowercased. They are case insensitive on GitHub's side, and
    without normalising them the unique constraint would happily accept both `Octocat` and
    `octocat` for the same person.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for(self, tracked_item_id: int, role: ActorRole) -> Sequence[ItemAssignment]:
        return (
            await self._session.scalars(
                select(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                )
            )
        ).all()

    async def replace(
        self, *, tracked_item_id: int, role: ActorRole, actors: Iterable[Actor]
    ) -> None:
        """Make the stored assignments for one role match GitHub.

        People no longer on the item are removed, so a reassigned pull request stops listing
        whoever used to be on it. People already there keep their row, and with it the
        `notified_at` that stops them being pinged twice.
        """
        wanted = {actor.login.lower() for actor in actors}
        existing = {row.github_username for row in await self.list_for(tracked_item_id, role)}

        removed = existing - wanted
        if removed:
            await self._session.execute(
                delete(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(removed),
                )
            )

        for login in sorted(wanted - existing):
            self._session.add(
                ItemAssignment(
                    tracked_item_id=tracked_item_id, github_username=login, role_type=role
                )
            )

        await self._session.flush()

    async def pending_notification(
        self, tracked_item_id: int, role: ActorRole
    ) -> Sequence[ItemAssignment]:
        return (
            await self._session.scalars(
                select(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.notified_at.is_(None),
                )
            )
        ).all()

    async def claim_notifications(self, tracked_item_id: int, role: ActorRole) -> Sequence[str]:
        """Take ownership of the pings nobody has sent yet, returning whose they are.

        Stamping first and posting after is what stops the same person being pinged twice. Two
        syncs of one item overlap whenever somebody runs /pr while an event for it is in
        flight, and a delivery that fails after the post is retried from the top. Both of those
        read the same pending rows if the read and the write are separate steps.
        """
        return (
            await self._session.scalars(
                update(ItemAssignment)
                .where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.notified_at.is_(None),
                )
                .values(notified_at=func.now())
                .returning(ItemAssignment.github_username)
                .execution_options(synchronize_session=False)
            )
        ).all()

    async def release_notifications(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str]
    ) -> None:
        """Hand claimed pings back, for when the message did not go out after all."""
        wanted = list(logins)
        if not wanted:
            return
        await self._session.execute(
            update(ItemAssignment)
            .where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                ItemAssignment.github_username.in_(wanted),
            )
            .values(notified_at=None)
            .execution_options(synchronize_session=False)
        )

    async def record_discord_ids(self, tracked_item_id: int, mentions: Mapping[str, int]) -> None:
        if not mentions:
            return
        for login, discord_user_id in mentions.items():
            await self._session.execute(
                update(ItemAssignment)
                .where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.github_username == login,
                )
                .values(discord_user_id=discord_user_id)
                .execution_options(synchronize_session=False)
            )

    async def clear_role_for(
        self, tracked_item_id: int, role: ActorRole, github_username: str
    ) -> bool:
        """Drop one person's assignment in one role, reporting whether there was one.

        GitHub drops a reviewer from `requested_reviewers` the moment they submit a review, and
        sends no `pull_request` event saying so. Following that here is what lets a later
        re-request read as a fresh request and ping them again.
        """
        result = await self._session.execute(
            delete(ItemAssignment).where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                ItemAssignment.github_username == github_username.lower(),
            )
        )
        return bool(result.rowcount)
