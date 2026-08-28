from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment
from shannon.domain.enums import ActorRole
from shannon.domain.models import Actor

logger = logging.getLogger(__name__)


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
        self,
        *,
        tracked_item_id: int,
        role: ActorRole,
        actors: Iterable[Actor],
        as_of: datetime | None = None,
    ) -> None:
        """Make the stored assignments for one role match GitHub.

        People no longer on the item lose their row. People already there keep theirs, and with
        it the `notified_at` that stops them being pinged twice.

        `as_of` is when GitHub says this payload was current, and it is what a new row records as
        the moment its request was made. Only on insert: a row that survives is the same request
        it always was, and moving its stamp forward on every unrelated event would erase the one
        thing that says how old it is.

        The insert settles its own conflict because two callers regularly sync one item at once:
        `/pr` runs while the worker is mid-delivery, GitHub sends several events for a new item
        together, and a second replica leases in parallel. Doing nothing on conflict keeps the
        other caller's row and whatever `notified_at` they have already claimed.
        """
        # Keyed by login and carrying the account id beside it. The id is what the ping path
        # checks a mention against, so it has to be recorded when the row is written, which is
        # the only moment the payload carrying it is in hand.
        wanted = {actor.login.lower(): actor.github_user_id for actor in actors}
        rows = await self._list_for(tracked_item_id, role)

        # Somebody who renamed their GitHub account is the same person, and matching on the name
        # alone read them as one person leaving and another arriving: the row was deleted and a
        # fresh one inserted with `notified_at` empty, so the next ordinary event on the item
        # announced a review request nobody had re-made. Where the new name was already linked,
        # that told the same person twice for one request, which is the one thing `notified_at`
        # exists to stop.
        #
        # Renames are followed by id where both sides have one, and the row takes the new name.
        by_id = {row.github_user_id: row for row in rows if row.github_user_id is not None}
        renamed = {
            login: by_id[found]
            for login, found in wanted.items()
            if found in by_id and by_id[found].github_username != login
        }
        for login, row in renamed.items():
            logger.info(
                "%r is now %r on GitHub, keeping the request row rather than asking again",
                row.github_username,
                login,
            )

        # What a renamed person's new row is written with. The rename drops the old row and
        # writes a fresh one carrying these forward, rather than giving the row the new name
        # where it sits. Two people on one item can swap names in a single payload, one taking
        # the name the other has just freed, and an update in place then breaks the unique
        # constraint on whichever of the two Postgres writes first: the delivery fails outright
        # and the item stops mirroring until sixteen attempts have run out. Clearing every old
        # name before writing any new one cannot, and needs no reasoning about the order.
        #
        # The row's own age is not carried, because nothing reads it. These four are what the
        # row is for.
        carried = {
            login: {
                "github_user_id": wanted[login],
                "requested_at": row.requested_at,
                "notified_at": row.notified_at,
                "fulfilled_at": row.fulfilled_at,
            }
            for login, row in renamed.items()
        }

        existing = {row.github_username for row in rows}
        removed = (existing - wanted.keys()) | {row.github_username for row in renamed.values()}
        if removed:
            await self._session.execute(
                delete(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(removed),
                )
            )

        # After the deletes, in one statement, so a name freed by anybody at all is available to
        # whoever is arriving at it.
        writing = [
            {
                "tracked_item_id": tracked_item_id,
                "github_username": login,
                "role_type": role,
                "github_user_id": wanted[login],
                "requested_at": as_of,
                "notified_at": None,
                "fulfilled_at": None,
            }
            for login in sorted(wanted.keys() - existing - renamed.keys())
        ] + [
            {
                "tracked_item_id": tracked_item_id,
                "github_username": login,
                "role_type": role,
                **stamps,
            }
            for login, stamps in sorted(carried.items())
        ]
        if writing:
            await self._session.execute(
                pg_insert(ItemAssignment)
                .values(writing)
                .on_conflict_do_nothing(constraint="uq_item_assignments_item_user_role")
            )

        await self._session.flush()

    async def claim_notifications(
        self, tracked_item_id: int, role: ActorRole
    ) -> dict[str, int | None]:
        """Take ownership of the pings nobody has sent yet, returning whose they are.

        Stamping first and posting after is what stops the same person being pinged twice. Two
        syncs of one item overlap whenever somebody runs /pr while an event for it is in
        flight, and a delivery that fails after the post is retried from the top. Both of those
        read the same pending rows if the read and the write are separate steps.

        Answered as login to account id, because this is the one place a mention is built from a
        stored name rather than from the payload in hand, and a name is not an identity: GitHub
        frees one when it is renamed or deleted and lets anybody take it. Without the id beside
        it, the ping is the mention a stranger inherits, and the ping is the one that notifies.
        Null for a row written before the column existed.
        """
        claimed = (
            await self._session.execute(
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
                .returning(ItemAssignment.github_username, ItemAssignment.github_user_id)
                .execution_options(synchronize_session=False)
            )
        ).mappings()
        return {row["github_username"]: row["github_user_id"] for row in claimed}

    async def release_notifications(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str]
    ) -> None:
        """Hand claimed pings back, for when the message did not go out after all.

        Folded, like every other login this class matches on. The column holds them folded,
        because `replace` is the only thing that writes it and folds on the way in, and the
        three other methods here that take logins all fold before comparing. This one did not,
        and it works today only because its one caller hands back exactly what
        `claim_notifications` returned, which came out of that column already folded. A caller
        passing what GitHub said would have matched no row, and matching no row here means a
        ping stamped as sent that nobody ever received, for good.
        """
        wanted = [login.lower() for login in logins]
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

    async def mark_fulfilled(
        self, tracked_item_id: int, role: ActorRole, github_username: str, when: datetime | None
    ) -> bool:
        """Record that the review this row asked for has been submitted.

        GitHub drops the reviewer from `requested_reviewers` on submit and sends no
        `pull_request` event saying so. Do not delete the row instead: a retried delivery
        replays a payload that still lists the reviewer, and would ping them to review what
        they just approved. `reopen_if_newer` compares a later request against this stamp,
        which is GitHub's clock, not ours.

        Never onto a request made since the review. This handler runs before its Discord post and
        again on every retry of the delivery, so a re-request made during a review delivery's
        backoff was closed a second time by the next attempt: the stamp read as an answered
        request, and the next ordinary event with a later timestamp reopened it and pinged for an
        ask nobody had made. `requested_at` is the row saying how old it is, on the same clock the
        review's own timestamp is on.
        """
        submitted = when or func.now()
        result = await self._session.execute(
            update(ItemAssignment)
            .where(
                ItemAssignment.tracked_item_id == tracked_item_id,
                ItemAssignment.role_type == role,
                ItemAssignment.github_username == github_username.lower(),
                or_(
                    ItemAssignment.requested_at.is_(None),
                    ItemAssignment.requested_at <= submitted,
                ),
            )
            .values(fulfilled_at=submitted)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def reopen_request(
        self, tracked_item_id: int, role: ActorRole, logins: Iterable[str], as_of: datetime | None
    ) -> Sequence[str]:
        """Hand back the ping on a request that has just been made again.

        `reopen_if_newer` covers the request a review closed here, by measuring a later payload
        against the stamp that closed it. This covers the one nothing here ever closed. GitHub
        drops a team from `requested_teams` the moment any member submits, and sends no
        `pull_request` event saying so, so the row survives with its ping already stamped. The
        next ask of that team arrives with the list unchanged, `replace` leaves the row alone,
        and the moment the whole feature exists for passes in silence. The same holds for a
        person whose review event never reached us.

        Only rows that were told, because a request nobody has been told about yet is already
        owed its ping and clearing an empty stamp says nothing.

        And only where the payload is newer than the request the row already holds, which is what
        makes a replayed delivery harmless: it carries the timestamp it always did. Both sides of
        that are GitHub's clock. A row with no stamp at all predates the column, and is reopened
        rather than refused: one repeated ping during the deployment that adds it beats a
        re-request that tells nobody for the life of every pull request already open.
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

        This is what a person clicking re-request looks like from here: the item comes back with
        the reviewer on it and a timestamp later than the review that closed the last request.
        A payload older than the review is a delivery catching up, and is left alone.

        Both stamps are cleared, because a reopened request has to be able to ping again, and
        the row records that it is now a request of this payload's age rather than the one the
        review closed.
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
