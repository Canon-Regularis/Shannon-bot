from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import delete, select
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
