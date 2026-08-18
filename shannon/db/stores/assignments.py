from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    async def _list_for(self, tracked_item_id: int, role: ActorRole) -> Sequence[ItemAssignment]:
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

        People no longer on the item lose their row. People already there keep theirs, and with
        it the `notified_at` that stops them being pinged twice.

        The insert settles its own conflict because two callers regularly sync one item at once:
        `/pr` runs while the worker is mid-delivery, GitHub sends several events for a new item
        together, and a second replica leases in parallel. Doing nothing on conflict keeps the
        other caller's row and whatever `notified_at` they have already claimed.
        """
        wanted = {actor.login.lower() for actor in actors}
        existing = {row.github_username for row in await self._list_for(tracked_item_id, role)}

        removed = existing - wanted
        if removed:
            await self._session.execute(
                delete(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(removed),
                )
            )

        added = sorted(wanted - existing)
        if added:
            await self._session.execute(
                pg_insert(ItemAssignment)
                .values(
                    [
                        {
                            "tracked_item_id": tracked_item_id,
                            "github_username": login,
                            "role_type": role,
                        }
                        for login in added
                    ]
                )
                .on_conflict_do_nothing(constraint="uq_item_assignments_item_user_role")
            )

        await self._session.flush()

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
                    # A request the review already answered is not owed a ping, even if the ping
                    # it was owed never went out. The reviewer failed to be told, then reviewed
                    # it anyway; telling them afterwards is worse than not telling them at all.
                    ItemAssignment.fulfilled_at.is_(None),
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

    async def mark_fulfilled(
        self, tracked_item_id: int, role: ActorRole, github_username: str, when: datetime | None
    ) -> bool:
        """Record that the review this row asked for has been submitted.

        GitHub drops the reviewer from `requested_reviewers` on submit and sends no
        `pull_request` event saying so. Do not delete the row instead: a retried delivery
        replays a payload that still lists the reviewer, and would ping them to review what
        they just approved. `reopen_if_newer` compares a later request against this stamp,
        which is GitHub's clock, not ours.
        """
        result = await self._session.execute(
            update(ItemAssignment)
            .where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                ItemAssignment.github_username == github_username.lower(),
            )
            .values(fulfilled_at=when or func.now())
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def reopen_if_newer(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str], as_of: datetime | None
    ) -> Sequence[str]:
        """Reopen requests that a payload newer than the review asks for again.

        This is what a person clicking re-request looks like from here: the item comes back with
        the reviewer on it and a timestamp later than the review that closed the last request.
        A payload older than the review is a delivery catching up, and is left alone.

        Both stamps are cleared, because a reopened request has to be able to ping again.
        """
        wanted = [login.lower() for login in logins]
        if not wanted or as_of is None:
            return ()
        return (
            await self._session.scalars(
                update(ItemAssignment)
                .where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(wanted),
                    ItemAssignment.fulfilled_at.is_not(None),
                    ItemAssignment.fulfilled_at < as_of,
                )
                .values(fulfilled_at=None, notified_at=None)
                .returning(ItemAssignment.github_username)
                .execution_options(synchronize_session=False)
            )
        ).all()
