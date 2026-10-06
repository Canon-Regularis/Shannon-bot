"""Whether a repository is private, written down and kept current.

Issue #98. The column is nullable and means "nobody said" when it is null, so the cases worth
pinning are the ones where something could quietly turn that into a claim: a row written before
the column existed, a payload that does not carry the flag, and a repository whose visibility
changes after it was registered.

It is recorded rather than acted on. Nothing branches on it today; it is here so that "is there
private code in this database" has an answer that does not require a GitHub call against a token
which may no longer have access.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType, Status
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

GUILD = 1
# Whoever linked the board in the tests that link one.
LINKER = 555


async def a_repository(session: AsyncSession, **overrides: object):
    arguments: dict[str, object] = {
        "github_repo_id": 500,
        "repo_name": "acme/widget",
        "repo_url": "https://github.com/acme/widget",
        "discord_guild_id": GUILD,
    }
    arguments.update(overrides)
    return await RepositoryStore(session).add(**arguments)


class TestWhatIsWrittenDownAtRegistration:
    async def test_a_private_repository_is_recorded_as_private(
        self, db_session: AsyncSession
    ) -> None:
        stored = await a_repository(db_session, private=True)

        assert stored.private is True

    async def test_a_public_one_is_recorded_as_public(self, db_session: AsyncSession) -> None:
        stored = await a_repository(db_session, private=False)

        assert stored.private is False

    async def test_a_repository_registered_without_the_answer_holds_no_answer(
        self, db_session: AsyncSession
    ) -> None:
        """Null rather than false. Every row written before this column existed is in exactly this
        state, and defaulting them to public would be a claim nobody checked."""
        stored = await a_repository(db_session)

        assert stored.private is None

    async def test_the_owners_account_is_recorded(self, db_session: AsyncSession) -> None:
        """Found reviewing #201: only the id tells a renamed account from a different one."""
        stored = await a_repository(db_session, owner_id=7)

        assert stored.github_owner_id == 7


class TestKeepingTheOwnerCurrent:
    """The owner's account id is learned and kept the way the visibility is. Found reviewing
    #201."""

    async def test_a_row_that_never_knew_learns_it_without_a_rename(
        self, db_session: AsyncSession
    ) -> None:
        stored = await a_repository(db_session)

        moved = await RepositoryStore(db_session).follow_rename(
            stored, repo_name="acme/widget", repo_url="https://github.com/acme/widget", owner_id=7
        )
        await db_session.commit()

        assert moved is False, "learning who owns it is not a rename"
        assert stored.github_owner_id == 7

    async def test_a_payload_that_does_not_say_leaves_what_is_known_alone(
        self, db_session: AsyncSession
    ) -> None:
        stored = await a_repository(db_session, owner_id=7)

        await RepositoryStore(db_session).follow_rename(
            stored, repo_name="acme/widget", repo_url="https://github.com/acme/widget"
        )

        assert stored.github_owner_id == 7


class TestARepositoryMovingToAnotherAccount:
    """Found reviewing #201. A board stored with no owner means "this repository's own owner", and
    a board number is a sequence GitHub keeps per account - so a transfer used to re-point the
    server, silently, at the new owner's board of the same number. Its board stays behind now,
    written down under the old owner, wherever nothing proves the account is the same one."""

    async def linked(
        self,
        session: AsyncSession,
        *,
        owner_id: int | None = 7,
        project_owner: str | None = None,
        board: int | None = 3,
        linker: int | None = LINKER,
    ) -> Repository:
        stored = await a_repository(session, owner_id=owner_id)
        stored.project_number = board
        stored.project_owner = project_owner
        # As `set_board` leaves it: nobody is recorded against a board that is not there.
        stored.project_linked_by = linker if board is not None else None
        await session.flush()
        return stored

    async def follow(
        self,
        session: AsyncSession,
        stored: Repository,
        *,
        to: str,
        owner_id: int | None,
        private: bool | None = None,
    ) -> bool:
        return await RepositoryStore(session).follow_rename(
            stored,
            repo_name=to,
            repo_url=f"https://github.com/{to}",
            private=private,
            owner_id=owner_id,
        )

    async def as_stored(self, session: AsyncSession, repository_id: int) -> Repository:
        """The row as the database has it, rather than as this session last saw it."""
        session.expire_all()
        found = await session.get(Repository, repository_id)
        assert found is not None
        return found

    async def test_the_board_stays_with_the_owner_it_was_linked_under(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored = await self.linked(db_session)

        with caplog.at_level(logging.WARNING, logger="shannon.db.stores.repositories"):
            moved = await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)

        assert moved is True
        assert (stored.repo_name, stored.project_owner) == ("someone-else/widget", "acme")
        assert stored.github_owner_id == 8
        assert stored.project_linked_by == LINKER, "a proven move is still the board they linked"
        assert "another account" in caplog.text
        assert "/board link" in caplog.text

    async def test_a_renamed_account_takes_its_board_along(self, db_session: AsyncSession) -> None:
        """The same id under a new login is the same account, and its board is still its own."""
        stored = await self.linked(db_session)

        await self.follow(db_session, stored, to="acme-renamed/widget", owner_id=7)

        assert stored.project_owner is None

    async def test_a_row_from_before_the_id_counts_as_a_move_and_learns_it(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not proven the same, so treated as not. And not proven different either - it may be the
        same account renamed, whose old login GitHub has released - so the board is taken off its
        linker as well, and nothing is read until somebody links it again."""
        stored = await self.linked(db_session, owner_id=None)

        with caplog.at_level(logging.WARNING, logger="shannon.db.stores.repositories"):
            await self.follow(db_session, stored, to="acme-renamed/widget", owner_id=7)

        assert (stored.project_owner, stored.github_owner_id) == ("acme", 7)
        assert stored.project_linked_by is None, "a board was left readable under a freed login"
        assert "nothing on record" in caplog.text
        assert "/board link" in caplog.text
        # /board unlink finds whose authorisation to let go of on the row, so after this it finds
        # nobody - and this line is the only place left that says whose is still held.
        assert f"Discord member {LINKER} linked it" in caplog.text
        # "Any they still hold": they may have withdrawn it already, and then nothing is held.
        assert "any authorisation they still hold in this server stays" in caplog.text
        assert "/board withdraw" in caplog.text

    async def test_a_board_with_nobody_recorded_against_it_names_nobody(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A board linked before #170 has no linker on the row, and the line says nothing about
        one rather than naming member None."""
        stored = await self.linked(db_session, owner_id=None, linker=None)

        with caplog.at_level(logging.WARNING, logger="shannon.db.stores.repositories"):
            await self.follow(db_session, stored, to="acme-renamed/widget", owner_id=7)

        assert "nothing on record" in caplog.text
        assert "Discord member" not in caplog.text

    async def test_a_payload_that_says_no_id_counts_as_a_move_and_forgets_the_old_id(
        self, db_session: AsyncSession
    ) -> None:
        """The id described the owner being left, so kept, it would pair the new name with the
        old account."""
        stored = await self.linked(db_session)

        await self.follow(db_session, stored, to="acme-renamed/widget", owner_id=None)

        assert (stored.project_owner, stored.github_owner_id) == ("acme", None)
        assert stored.project_linked_by is None

    async def test_a_row_and_a_payload_that_both_say_no_id_count_as_a_move(
        self, db_session: AsyncSession
    ) -> None:
        """Two unknowns are not a match: None and None prove nothing about the account."""
        stored = await self.linked(db_session, owner_id=None)

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=None)

        assert (stored.project_owner, stored.github_owner_id) == ("acme", None)
        assert stored.project_linked_by is None

    async def test_an_older_name_put_back_without_an_id_cannot_prove_a_later_move_harmless(
        self, db_session: AsyncSession
    ) -> None:
        """A repository payload whose owner carries no usable id - an `id` missing or not a number,
        or no owner at all, so the login comes from `full_name` - can put an older name back after
        a delivery renamed the row. Kept, the id that delivery learned would sit beside the old
        name, and once somebody ran /board link again the next delivery would read it as the same
        account and re-point the board. A draft card no longer gets here at all: see
        test_a_pass_that_copied_the_row_before_a_rename_does_not_put_the_old_name_back."""
        stored = await self.linked(db_session)
        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)
        await self.follow(db_session, stored, to="acme/widget", owner_id=None)
        await RepositoryStore(db_session).set_board(
            stored, project_number=3, project_owner=None, linked_by=LINKER
        )

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)

        assert stored.project_owner == "acme"

    async def test_a_board_linked_while_the_delivery_was_reading_is_not_overwritten(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The delivery reads the row without a lock. A /board link committing after that read
        named an owner, and the pin decided from the old read would overwrite it - so it asks
        again of the row as it is now, held."""
        stored = await self.linked(db_session)
        await db_session.commit()
        async with db_sessionmaker() as other, other.begin():
            meanwhile = await other.get(Repository, stored.id)
            assert meanwhile is not None
            meanwhile.project_number = 2
            meanwhile.project_owner = "carol"

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)
        await db_session.commit()

        assert (stored.project_number, stored.project_owner) == (2, "carol")

    async def test_a_board_linked_after_the_delivery_read_none_stays_behind_too(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other way round. The delivery read a row with no board, and a /board link committed
        after that read. Deciding from the read whether to look again, it would never look - and
        would carry the board it never saw to the new owner, whose board of that number would then
        be read under the linker's authorisation."""
        stored = await self.linked(db_session, board=None)
        await db_session.commit()
        repository_id = stored.id
        async with db_sessionmaker() as other, other.begin():
            meanwhile = await self.as_stored(other, repository_id)
            await RepositoryStore(other).set_board(
                meanwhile, project_number=3, project_owner=None, linked_by=LINKER
            )

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.project_number, row.project_owner) == (3, "acme")
        assert row.project_linked_by == LINKER, "a proven move is still the board they linked"

    async def test_a_board_link_still_in_flight_is_waited_for(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Reading the row again is not enough by itself: a /board link committing between that
        read and the rename would be carried along all the same. So the read holds the row, and a
        link still in flight is waited out, then seen."""
        stored = await self.linked(db_session, board=None)
        await db_session.commit()
        repository_id = stored.id

        async with db_sessionmaker() as linking:
            await linking.begin()
            meanwhile = await self.as_stored(linking, repository_id)
            await RepositoryStore(linking).set_board(
                meanwhile, project_number=3, project_owner=None, linked_by=LINKER
            )
            # Started while the link holds the row uncommitted, so the move meets it rather than
            # racing it by luck.
            moving = asyncio.create_task(
                self.follow(db_session, stored, to="someone-else/widget", owner_id=8)
            )
            await blocked_on_a_row(db_sessionmaker, moving)
            await linking.commit()
            await moving
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.project_number, row.project_owner) == (3, "acme")

    async def test_a_late_rename_under_the_old_owner_keeps_its_name_and_id_together(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two deliveries for two items, overlapping: one carries a rename acme made just before
        transferring the repository, the other the transfer. The first read the row before the
        second committed, and a write decided from that read would put acme's name beside the new
        owner's id - which the next move then takes as proof of the same account, carrying a board
        linked in between to the new owner. Read again, held, the name and the id go together."""
        stored = await self.linked(db_session)
        await db_session.commit()
        repository_id = stored.id
        async with db_sessionmaker() as other, other.begin():
            transferred = await self.as_stored(other, repository_id)
            await self.follow(other, transferred, to="someone-else/gadget", owner_id=8)

        await self.follow(db_session, stored, to="acme/gadget", owner_id=7)
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.repo_name, row.github_owner_id) == ("acme/gadget", 7)
        # Linked again with no owner while the row says acme, then the new owner's next delivery:
        # the ids prove another account, so the board stays acme's.
        await RepositoryStore(db_session).set_board(
            row, project_number=3, project_owner=None, linked_by=LINKER
        )
        await self.follow(db_session, row, to="someone-else/gadget", owner_id=8)
        assert row.project_owner == "acme"

    async def test_a_late_first_id_under_the_old_owner_is_not_paired_with_the_new_name(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same with only the id to learn: a row from before the column, whose first delivery
        since says who owns it just as a transfer commits."""
        stored = await self.linked(db_session, owner_id=None)
        await db_session.commit()
        repository_id = stored.id
        async with db_sessionmaker() as other, other.begin():
            transferred = await self.as_stored(other, repository_id)
            await self.follow(other, transferred, to="someone-else/widget", owner_id=8)

        await self.follow(db_session, stored, to="acme/widget", owner_id=7)
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.repo_name, row.github_owner_id) == ("acme/widget", 7)

    async def test_a_board_unlinked_while_a_move_kept_it_behind_goes_whole(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """/board unlink read the board before a transfer's delivery wrote the old owner onto it.
        Clearing only the columns that read saw set would leave that owner behind, on no board."""
        stored = await self.linked(db_session)
        await db_session.commit()
        repository_id = stored.id
        async with db_sessionmaker() as other, other.begin():
            transferred = await self.as_stored(other, repository_id)
            await self.follow(other, transferred, to="someone-else/widget", owner_id=8)

        await RepositoryStore(db_session).set_board(stored, project_number=None, project_owner=None)
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.project_number, row.project_owner, row.project_linked_by) == (None, None, None)

    async def test_a_new_item_being_written_does_not_hold_the_move_up(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Writing a new item takes KEY SHARE on its repository's row through the foreign key,
        and holds it until that sync commits. FOR UPDATE would wait on that - so after a transfer,
        two syncs of two new items, each holding one and each waiting on the other's, would
        deadlock, and one would be thrown away. The lock taken here is the one the rename's own
        UPDATE takes, which does not wait on it."""
        stored = await self.linked(db_session)
        await db_session.commit()
        repository_id = stored.id

        async with db_sessionmaker() as writing, writing.begin():
            await TrackedItemStore(writing).get_or_create(
                repository_id=repository_id,
                object_type=ObjectType.ISSUE,
                github_object_id=41,
                github_object_number=41,
                github_url="https://github.com/acme/widget/issues/41",
                title="Opened in the same breath",
                github_state="open",
                status=Status.NOT_REVIEWED,
            )
            # Nothing else holds the row, so any wait at all is this one, and it never ends on
            # its own: the writer above commits only once the move has.
            await db_session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)
            await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.repo_name, row.project_owner) == ("someone-else/widget", "acme")

    async def test_a_move_that_also_changes_the_visibility_keeps_both(
        self, db_session: AsyncSession
    ) -> None:
        """The row is read again before anything is set, because a refresh puts back whatever is
        unflushed: after the visibility, it would undo it while the log said it had changed."""
        stored = await self.linked(db_session)
        await db_session.commit()
        repository_id = stored.id

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8, private=True)
        await db_session.commit()

        row = await self.as_stored(db_session, repository_id)
        assert (row.private, row.project_owner) == (True, "acme")

    async def test_a_board_named_by_its_owner_is_never_rewritten(
        self, db_session: AsyncSession
    ) -> None:
        stored = await self.linked(db_session, project_owner="boards-inc")

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)

        assert stored.project_owner == "boards-inc"

    async def test_a_repository_with_no_board_has_nothing_to_leave_behind(
        self, db_session: AsyncSession
    ) -> None:
        stored = await self.linked(db_session, board=None)

        await self.follow(db_session, stored, to="someone-else/widget", owner_id=8)

        assert stored.project_owner is None

    async def test_a_change_of_case_is_not_a_move(self, db_session: AsyncSession) -> None:
        """GitHub logins ignore case, and a payload can spell the owner either way."""
        stored = await self.linked(db_session, owner_id=None)

        await self.follow(db_session, stored, to="ACME/widget", owner_id=None)

        assert stored.project_owner is None

    async def test_a_change_of_case_with_no_id_keeps_the_id(self, db_session: AsyncSession) -> None:
        """Not a move, so the id still describes the owner. Forgotten, the next real move would be
        judged unproven and take the board off its linker for nothing."""
        stored = await self.linked(db_session)

        await self.follow(db_session, stored, to="ACME/widget", owner_id=None)

        assert stored.github_owner_id == 7

    async def test_a_renamed_repository_under_the_same_owner_is_not_a_move(
        self, db_session: AsyncSession
    ) -> None:
        stored = await self.linked(db_session, owner_id=None)

        await self.follow(db_session, stored, to="acme/gadget", owner_id=None)

        assert stored.project_owner is None


class TestKeepingItCurrent:
    async def test_a_repository_that_went_private_is_noticed(
        self, db_session: AsyncSession
    ) -> None:
        """This is what makes the column self-healing. Every delivery passes through here, so a
        row that was null or stale corrects itself during ordinary use rather than needing a
        backfill that would have to guess."""
        stored = await a_repository(db_session, private=False)

        await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/widget",
            repo_url="https://github.com/acme/widget",
            private=True,
        )

        assert stored.private is True

    async def test_a_row_that_never_knew_learns_on_the_next_delivery(
        self, db_session: AsyncSession
    ) -> None:
        stored = await a_repository(db_session)

        await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/widget",
            repo_url="https://github.com/acme/widget",
            private=True,
        )

        assert stored.private is True

    async def test_a_payload_that_does_not_say_leaves_what_is_known_alone(
        self, db_session: AsyncSession
    ) -> None:
        """Only written where GitHub actually said. A less informative delivery must not erase a
        better-informed one, which is the same rule the installation store follows for an id."""
        stored = await a_repository(db_session, private=True)

        await RepositoryStore(db_session).follow_rename(
            stored, repo_name="acme/widget", repo_url="https://github.com/acme/widget"
        )

        assert stored.private is True

    async def test_a_visibility_change_is_not_a_rename(self, db_session: AsyncSession) -> None:
        """The answer drives a rename log and a re-render of stored links. A repository quietly
        flipped to private has not been renamed, and saying it was would send somebody looking for
        a name change that never happened."""
        stored = await a_repository(db_session, private=False)

        moved = await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/widget",
            repo_url="https://github.com/acme/widget",
            private=True,
        )

        assert moved is False

    async def test_a_rename_still_reads_as_a_rename(self, db_session: AsyncSession) -> None:
        stored = await a_repository(db_session, private=False)

        moved = await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/gadget",
            repo_url="https://github.com/acme/gadget",
            private=False,
        )

        assert moved is True
        assert stored.repo_name == "acme/gadget"

    async def test_both_at_once_are_both_taken(self, db_session: AsyncSession) -> None:
        stored = await a_repository(db_session, private=False)

        moved = await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/gadget",
            repo_url="https://github.com/acme/gadget",
            private=True,
        )

        assert moved is True
        assert stored.private is True

    async def test_nothing_changing_changes_nothing(self, db_session: AsyncSession) -> None:
        stored = await a_repository(db_session, private=True)

        moved = await RepositoryStore(db_session).follow_rename(
            stored,
            repo_name="acme/widget",
            repo_url="https://github.com/acme/widget",
            private=True,
        )

        assert moved is False
        assert stored.private is True

    async def test_going_private_is_said_out_loud(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A repository changing visibility under a running mirror is worth a line. It changes
        what is being copied into a Discord channel, and nothing else would report it."""
        stored = await a_repository(db_session, private=False)

        with caplog.at_level(logging.INFO):
            await RepositoryStore(db_session).follow_rename(
                stored,
                repo_name="acme/widget",
                repo_url="https://github.com/acme/widget",
                private=True,
            )

        assert "now private" in caplog.text

    async def test_going_public_is_said_too(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored = await a_repository(db_session, private=True)

        with caplog.at_level(logging.INFO):
            await RepositoryStore(db_session).follow_rename(
                stored,
                repo_name="acme/widget",
                repo_url="https://github.com/acme/widget",
                private=False,
            )

        assert "now public" in caplog.text
