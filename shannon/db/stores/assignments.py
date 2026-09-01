from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
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
        mine = self._match(wanted, rows)

        # Whoever is left over is not on the item any more, under any name. Their names are
        # freed here, before any rename below is allowed to take one.
        kept = {row.id for row in mine.values() if row is not None}
        removed = sorted(row.github_username for row in rows if row.id not in kept)
        if removed:
            await self._session.execute(
                delete(ItemAssignment).where(
                    ItemAssignment.tracked_item_id == tracked_item_id,
                    ItemAssignment.role_type == role,
                    ItemAssignment.github_username.in_(removed),
                )
            )

        for login, row in mine.items():
            # The payload knows who this login is. A row that predates the column, or one matched
            # by name because neither side had an id, learns it here and stops being a guess from
            # the next event onwards.
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
    def _match(
        wanted: dict[str, int | None], rows: Sequence[ItemAssignment]
    ) -> dict[str, ItemAssignment | None]:
        """Which stored row, if any, belongs to each person the payload names.

        Somebody who renamed their GitHub account is the same person, and matching on the name
        alone read them as one person leaving and another arriving: the row was deleted and a
        fresh one inserted with `notified_at` empty, so the next ordinary event on the item
        announced a review request nobody had re-made. Where the new name was already linked,
        that told the same person twice for one request, which is the one thing `notified_at`
        exists to stop.

        So the account id goes first, for everybody who has one, before a single name is looked
        at. A name match is what happens when the id cannot answer, and it must not take a row
        the id has already spoken for.

        A row carrying a different account under the name being asked about belongs to somebody
        else, and the name is all the two have in common. It is left unmatched, which drops it,
        because GitHub frees a login the moment it is left and a row saying otherwise is out of
        date. Getting this wrong is not loud: the row survives, no later payload rewrites the id
        on a login that already exists, and the item goes on addressing the account that left for
        the rest of its life.
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
            if row is None or row.id in claimed:
                mine[login] = None
                continue
            mine[login] = row
            claimed.add(row.id)
        return mine

    async def _rename(self, renamed: dict[str, ItemAssignment], *, held: set[str]) -> None:
        """Give each row the name its account goes by now, in an order that cannot collide.

        One column at a time, rather than dropping the row and writing a replacement. The
        replacement carried the stamps forward in Python, which turned a rename into a read of
        the whole row followed by a write of it, and anything another transaction committed in
        between was reverted: a ping handed back, a review recorded, the stamp that stops
        somebody being told twice. Nothing serialises those against this, deliberately, because
        the notifier runs after the sync transaction has committed. A statement naming one column
        has no such window.

        The order is the whole difficulty. Two people on one item can swap names in a single
        payload, because GitHub frees a name the moment it is left: one renames, the other takes
        what they left, and both changes arrive on the next event. Writing a name another row is
        still holding breaks the unique constraint, which raises out of the delivery and stops
        the item mirroring at all until sixteen attempts have run out.

        So a rename goes only when nothing is holding the name it wants. The deletions have
        already run, and each rename frees its old name for the next, which is enough for any
        chain of them. A closed loop, where two accounts have traded names outright, has no first
        move: one of them is parked on a name GitHub cannot issue, since a login can neither
        begin with a hyphen nor contain two in a row, and the row is renamed off it again before
        this returns.
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
        self, tracked_item_id: int, role: ActorRole, people: Mapping[str, int | None]
    ) -> None:
        """Hand claimed pings back, for when the message did not go out after all.

        By account id wherever the claim had one, because the login is not stable across the gap
        this covers. The gap is the whole Discord post: the worker allows each delivery sixty
        seconds and discord.py sleeps through a rate limit rather than failing, so a second
        delivery carrying a rename has time to commit in the middle of it. Matching on the name
        the claim went out under then found no row at all, and finding no row here means a ping
        stamped as sent that nobody ever received, for good. Where two people on one item had
        traded names it was worse than nothing: the hand-back cleared the stamp of whoever now
        holds the released name, so one of them was told twice and the other never.

        Falling back to the name for anybody the claim had no id for, which is a row written
        before the column existed or an account GitHub no longer has. Folded, like every other
        login this class matches on, because the column holds them folded.
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
        `pull_request` event saying so. Do not delete the row instead: a retried delivery
        replays a payload that still lists the reviewer, and would ping them to review what
        they just approved. `reopen_if_newer` compares a later request against this stamp,
        which is GitHub's clock, not ours.

        Matched by account where the row knows one, because the two sides of this comparison are
        further apart in time than they look. The row carries the name the reviewer had when
        GitHub asked them; the review carries the name they have now. Nothing between the two
        updates the row, since a rename reaches this bot on the next `pull_request` event and
        submitting a review does not send one. So a reviewer who renamed closed nothing, and the
        request stayed open with the ping still owed: the next ordinary event asked them to review
        what they had already reviewed, which is the one thing this stamp exists to stop.

        Never onto a request made since the review. This handler runs before its Discord post and
        again on every retry of the delivery, so a re-request made during a review delivery's
        backoff was closed a second time by the next attempt: the stamp read as an answered
        request, and the next ordinary event with a later timestamp reopened it and pinged for an
        ask nobody had made. `requested_at` is the row saying how old it is, on the same clock the
        review's own timestamp is on.
        """
        submitted = when or func.now()
        by_name = ItemAssignment.github_username == github_username.lower()
        # A deleted account arrives with no id, and a row written before the column existed has
        # none either. Both fall back to the name, which is what they were matched on before.
        same_person = (
            by_name
            if account is None
            else or_(
                ItemAssignment.github_user_id == account,
                and_(ItemAssignment.github_user_id.is_(None), by_name),
            )
        )
        result = await self._session.execute(
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
