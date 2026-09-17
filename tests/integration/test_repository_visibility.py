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

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.stores.repositories import RepositoryStore

pytestmark = pytest.mark.integration

GUILD = 1


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
