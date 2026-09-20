from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import rows_changed
from shannon.db.models import ItemAssignment
from shannon.domain.enums import ActorRole
from shannon.domain.models import Actor
from shannon.domain.time import as_utc

logger = logging.getLogger(__name__)


class ItemAssignmentStore:
    """Logins are stored folded.

    GitHub treats them case insensitively, so without folding the unique constraint would accept
    both `Octocat` and `octocat` for the same person.
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
        self,
        *,
        tracked_item_id: int,
        role: ActorRole,
        actors: Iterable[Actor],
        as_of: datetime | None = None,
    ) -> None:
        """Make the stored assignments for one role match GitHub.

        A surviving row keeps its `notified_at`, so one request never pings twice. `as_of` is
        stamped on insert only: moving it forward on an unrelated event would erase how old the
        request is. Two callers regularly sync one item at once, `/pr` against a mid-delivery
        worker, several events for one new item, a second replica leasing in parallel, so the
        insert does nothing on conflict and keeps the row and claim the other caller holds.
        """
        # The id is what the ping path checks a mention against, and this payload is the only
        # place it appears, so it has to be recorded as the row is written.
        wanted = {actor.login.lower(): actor.github_user_id for actor in actors}
        rows = await self._list_for(tracked_item_id, role)
        mine = self._match(wanted, rows)

        # Deleting first frees these names, before any rename below is allowed to take one.
        kept = {row.id for row in mine.values() if row is not None}
        removed = sorted(
            row.github_username
            for row in rows
            if row.id not in kept and not self._already_told_and_newer(row, as_of)
        )
        if removed:
            await self._session.execute(
                delete(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(removed),
                )
            )

        for login, row in mine.items():
            # A row that predates the column, or one matched by name, learns its account here
            # and stops being a guess from the next event onwards.
            if row is not None and row.github_user_id is None and wanted[login] is not None:
                row.github_user_id = wanted[login]

        await self._rename(
            {
                login: row
                for login, row in mine.items()
                if row is not None and row.github_username != login
            },
            held={row.github_username for row in rows if row.id in kept},
        )

        added = sorted(login for login, row in mine.items() if row is None)
        if added:
            await self._session.execute(
                pg_insert(ItemAssignment)
                .values(
                    [
                        {
                            "tracked_item_id": tracked_item_id,
                            "github_username": login,
                            "github_user_id": wanted[login],
                            "role_type": role,
                            "requested_at": as_of,
                        }
                        for login in added
                    ]
                )
                .on_conflict_do_nothing(constraint="uq_item_assignments_item_user_role")
            )

    @staticmethod
    def _already_told_and_newer(row: ItemAssignment, as_of: datetime | None) -> bool:
        """Whether this payload is too old to be asked to remove this row.

        GitHub stamps `pull_request.updated_at` to the second, so two deliveries milliseconds
        apart carry the same one, and the worker can run them newest first: a transient Discord
        error puts a delivery behind the one after it, since the lease skips a row whose next
        attempt is in the future. The older payload would then delete the row the newer one made,
        taking `notified_at` with it, and the next ordinary event puts the person back and pings
        them for a review nobody asked for twice. Only rows somebody has been pinged from, since
        one nobody was told about is re-added by the next delivery and pinged once. Equal counts
        as too old, at the cost of a reviewer removed in that same second staying until the next
        event.
        """
        if row.notified_at is None or as_of is None or row.requested_at is None:
            return False
        return as_utc(row.requested_at) >= as_utc(as_of)

    @staticmethod
    def _match(
        wanted: dict[str, int | None], rows: Sequence[ItemAssignment]
    ) -> dict[str, ItemAssignment | None]:
        """Which stored row, if any, belongs to each person the payload names.

        The account id matches first because a renamed account is the same person: on the name
        alone the row is deleted and reinserted with `notified_at` empty, and the next ordinary
        event announces a request nobody re-made, or tells the same person twice where the new
        name was already on the item. A row holding a different account under the login being
        asked about is somebody else, since GitHub frees a login the moment it is left, so it is
        left unmatched and dropped rather than kept addressing an account that has gone.
        """
        by_id = {row.github_user_id: row for row in rows if row.github_user_id is not None}
        by_name = {row.github_username: row for row in rows}
        mine: dict[str, ItemAssignment | None] = {}
        claimed: set[int] = set()

        for login, account in wanted.items():
            row = by_id.get(account) if account is not None else None
            if row is not None:
                mine[login] = row
                claimed.add(row.id)

        for login, account in wanted.items():
            if login in mine:
                continue
            row = by_name.get(login)
            disputed = (
                row is not None
                and account is not None
                and row.github_user_id is not None
                and row.github_user_id != account
            )
            if row is None or disputed or row.id in claimed:
                mine[login] = None
                continue
            mine[login] = row
            claimed.add(row.id)
        return mine

    async def _rename(self, renamed: dict[str, ItemAssignment], *, held: set[str]) -> None:
        """Give each row the name its account goes by now, in an order that cannot collide.

        One column at a time, not a delete and a reinsert: the reinsert carried the stamps
        forward in Python, so anything another transaction committed in between was reverted, and
        nothing serialises the notifier against this because it runs after the sync transaction
        commits. Two people on one item can swap names in a single payload, since GitHub frees a
        name the moment it is left, and writing a name another row still holds breaks the unique
        constraint and raises out of the delivery. So a rename goes only when nothing holds the
        name it wants; a closed loop has no first move, so one row is parked on a `--swap-` name,
        which GitHub cannot issue because a login can neither begin with a hyphen nor contain two
        in a row.
        """
        while renamed:
            ready = sorted(login for login in renamed if login not in held)
            if not ready:
                login, row = sorted(renamed.items())[0]
                parked = f"--swap-{row.id}"
                logger.info(
                    "%r and %r have traded names, parking one of them to make room",
                    row.github_username,
                    login,
                )
                held.discard(row.github_username)
                held.add(parked)
                row.github_username = parked
            else:
                for login in ready:
                    row = renamed.pop(login)
                    logger.info(
                        "%r is now %r on GitHub, keeping the request row rather than asking again",
                        row.github_username,
                        login,
                    )
                    held.discard(row.github_username)
                    held.add(login)
                    row.github_username = login
            await self._session.flush()

    async def claim_notifications(
        self, tracked_item_id: int, role: ActorRole
    ) -> dict[str, int | None]:
        """Take ownership of the pings nobody has sent yet, returning whose they are.

        Stamping and reading in one statement is what stops a double ping: two syncs of one item
        overlap whenever somebody runs /pr while an event for it is in flight, and a delivery
        that fails after the post is retried from the top. Answered as login to account id
        because this is the one place a mention is built from a stored name rather than from the
        payload in hand, and GitHub frees a renamed or deleted login for anybody to take.
        """
        claimed = (
            await self._session.execute(
                update(ItemAssignment)
                .where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.notified_at.is_(None),
                    # A request the review already answered is not owed a ping, even if the
                    # ping it was owed never went out.
                    ItemAssignment.fulfilled_at.is_(None),
                )
                .values(notified_at=func.now())
                .returning(ItemAssignment.github_username, ItemAssignment.github_user_id)
                .execution_options(synchronize_session=False)
            )
        ).mappings()
        return {row["github_username"]: row["github_user_id"] for row in claimed}

    async def release_notifications(
        self, tracked_item_id: int, role: ActorRole, people: Mapping[str, int | None]
    ) -> None:
        """Hand claimed pings back, for when the message did not go out after all.

        By account id wherever the claim had one, because a rename can commit inside the gap this
        covers: the worker allows each delivery sixty seconds and discord.py sleeps through a
        rate limit rather than failing. Matching on the name the claim went out under then found
        no row, leaving a ping stamped as sent that nobody ever received, or, where two people
        had traded names, cleared the stamp of the wrong one.
        """
        if not people:
            return
        # Everybody lands in exactly one of these, so between them they are never both empty.
        ids = sorted({account for account in people.values() if account is not None})
        logins = sorted(login.lower() for login, account in people.items() if account is None)
        await self._session.execute(
            update(ItemAssignment)
            .where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                or_(
                    ItemAssignment.github_user_id.in_(ids),
                    ItemAssignment.github_username.in_(logins),
                ),
            )
            .values(notified_at=None)
            .execution_options(synchronize_session=False)
        )

    async def mark_fulfilled(
        self,
        tracked_item_id: int,
        role: ActorRole,
        github_username: str,
        when: datetime | None,
        account: int | None = None,
    ) -> bool:
        """Record that the review this row asked for has been submitted.

        GitHub drops the reviewer from `requested_reviewers` on submit and sends no
        `pull_request` event saying so. Do not delete the row instead: a retried delivery replays
        a payload that still lists the reviewer and would ping them to review what they just
        approved. Matched by account where the row knows one, because the row carries the name
        the reviewer had when GitHub asked them and nothing updates it in between: a rename
        reaches this bot on the next `pull_request` event, and submitting a review sends none.
        Never onto a request made since the review, since this handler runs again on every retry
        of the delivery and would otherwise close a re-request made during the backoff. Both
        stamps are on GitHub's clock, not ours.
        """
        submitted = when or func.now()
        by_name = ItemAssignment.github_username == github_username.lower()
        # A deleted account arrives with no id, and a row written before the column existed has
        # none either. Both fall back to the name.
        same_person = (
            by_name
            if account is None
            else or_(
                ItemAssignment.github_user_id == account,
                and_(ItemAssignment.github_user_id.is_(None), by_name),
            )
        )
        changed = await rows_changed(
            self._session,
            update(ItemAssignment)
            .where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                same_person,
                or_(
                    ItemAssignment.requested_at.is_(None),
                    ItemAssignment.requested_at <= submitted,
                ),
            )
            .values(fulfilled_at=submitted)
            .execution_options(synchronize_session=False),
        )
        return bool(changed)

    async def reopen_request(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str], as_of: datetime | None
    ) -> Sequence[str]:
        """Hand back the ping on a request that has just been made again.

        GitHub drops a team from `requested_teams` the moment any member submits and sends no
        `pull_request` event saying so, so the row survives with its ping already stamped, the
        next ask of that team arrives with the list unchanged, and `replace` leaves it alone. The
        same holds for a person whose review event never reached us; the request a review closed
        here is `reopen_if_newer`'s. Only where the payload is newer than the request the row
        holds, both on GitHub's clock, which is what makes a replayed delivery harmless. A row
        with no stamp at all predates the column and is reopened rather than refused.
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
                    ItemAssignment.notified_at.is_not(None),
                    or_(
                        ItemAssignment.requested_at.is_(None),
                        ItemAssignment.requested_at < as_of,
                    ),
                )
                .values(fulfilled_at=None, notified_at=None, requested_at=as_of)
                .returning(ItemAssignment.github_username)
                .execution_options(synchronize_session=False)
            )
        ).all()

    async def reopen_if_newer(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str], as_of: datetime | None
    ) -> Sequence[str]:
        """Reopen requests that a payload newer than the review asks for again.

        A person clicking re-request brings the item back with the reviewer on it and a timestamp
        later than the review that closed the last request. A payload older than the review is a
        delivery catching up, and is left alone.
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
                .values(fulfilled_at=None, notified_at=None, requested_at=as_of)
                .returning(ItemAssignment.github_username)
                .execution_options(synchronize_session=False)
            )
        ).all()
