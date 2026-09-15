"""Tagging somebody in a GitHub comment reaches them in Discord.

A comment named its author as a mention and everybody else as plain text, so `@John, can you look
at this?` reached John on GitHub and reached nobody in the one place the team is actually reading.
Issue #63.

Through the whole stack rather than against the renderer, because the half that decides which
names to look up lives in the note path and the half that swaps them lives in the renderer, and a
test of either alone cannot tell whether they agree about what was named.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration

# The child in a sub-issue pair: its own number, its own id, and a parent hanging off it the way
# GitHub sends one. Nothing reads that key, which is the point of the test that uses it.
SUB_ISSUE = {
    "id": 9001,
    "number": 57,
    "title": "Child task",
    "html_url": "https://github.com/Canon-Regularis/Shannon-bot/issues/57",
    "parent": {"id": 8000, "number": 12, "title": "Auth rewrite"},
}


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        yield http_client


async def link_person(session: AsyncSession, login: str, *, github_user_id: int, discord: int):
    await UserLinkStore(session).link(
        guild_id=1,
        github_username=login,
        github_user_id=github_user_id,
        discord_user_id=discord,
    )
    await session.commit()


def last_post(threads: FakeThreadGateway) -> str:
    return threads.posts[-1][1]


async def test_a_linked_name_in_a_body_is_mentioned(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await link_person(db_session, "hubot", github_user_id=100, discord=909)

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="can you look at this, @hubot?"),
        delivery="c1",
    )

    assert "<@909>" in last_post(threads)


async def test_a_name_nobody_linked_is_left_as_written(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="can you look at this, @hubot?"),
        delivery="c1",
    )

    posted = last_post(threads)
    assert "@hubot" in posted
    assert "<@" not in posted.split("\n")[1]


async def test_a_linked_team_in_a_body_pings_the_role(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await TeamLinkStore(db_session).link(guild_id=1, github_team="backend", discord_role_id=4242)
    await db_session.commit()

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="cc @canon-regularis/backend on this"),
        delivery="c1",
    )

    assert "<@&4242>" in last_post(threads)


async def test_a_comment_from_a_deleted_account_still_reaches_who_it_names(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """GitHub sends a null user for an account that has since been deleted, so there is nobody to
    name in the header. The people that comment asked for are still somebody, and the lookup has
    to be asked about them rather than skipped along with the author.
    """
    await link_person(db_session, "hubot", github_user_id=100, discord=909)

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="over to you @hubot", user=None),
        delivery="c1",
    )

    posted = last_post(threads)
    assert posted.startswith("**Unknown** commented")
    assert "<@909>" in posted


async def test_a_mass_mention_still_reaches_nobody(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="@everyone drop what you are doing"),
        delivery="c1",
    )

    assert "@everyone" not in last_post(threads)


class TestASubIssue:
    """A sub-issue is an ordinary issue and nothing here treats it otherwise.

    GitHub's sub-issues are a relationship between two issues rather than a new kind of object:
    the child has its own number, its own `issues.opened`, its own thread and its own comment
    stream, and the only thing that says it is a child is a `parent` key nothing reads. So the
    proof that tagging works on one is a test that does nothing special.
    """

    async def test_it_gets_its_own_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(
            tracked, "issues", payloads.issue_event("opened", **SUB_ISSUE), delivery="i-child"
        )

        assert len(threads.created) == 2, "the child did not get a thread of its own"

    async def test_a_tag_in_a_comment_on_it_reaches_the_person(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await link_person(db_session, "hubot", github_user_id=100, discord=909)
        await deliver(
            tracked, "issues", payloads.issue_event("opened", **SUB_ISSUE), delivery="i-child"
        )
        child_thread = threads.created[-1].thread_id

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(on=payloads.issue(**SUB_ISSUE), body="over to you @hubot"),
            delivery="c-child",
        )

        thread_id, posted = threads.posts[-1]
        assert thread_id == child_thread, "the comment went somewhere other than the sub-issue"
        assert "<@909>" in posted


class TestALoginThatChangedHands:
    """The one case where the author and the body are treated differently, on purpose.

    A payload names its author with a GitHub id beside the login, so a login that has been freed
    and taken by somebody else can be caught. A name read out of a body carries no id at all, so
    that check cannot run on it and the store's own rule applies: an asked null is no evidence,
    and refusing on no evidence would take away mentions that work.
    """

    async def test_the_author_is_not_mentioned_when_the_id_disagrees(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await link_person(db_session, "monalisa", github_user_id=200, discord=909)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body="hello", user=payloads.user("monalisa", 777)),
            delivery="c1",
        )

        assert "<@909>" not in last_post(threads), "a stranger inherited somebody else's mention"

    async def test_a_name_in_the_body_is_still_mentioned(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Surprising enough to be asserted deliberately rather than discovered later."""
        await link_person(db_session, "hubot", github_user_id=100, discord=909)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body="ask @hubot"),
            delivery="c1",
        )

        assert "<@909>" in last_post(threads)

    async def test_an_author_naming_themselves_is_judged_on_the_payload(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The author's entry has to win over the one read out of the body, or writing your own
        name in your own comment would quietly downgrade the only verified id on this path."""
        await link_person(db_session, "monalisa", github_user_id=200, discord=909)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(
                body="as @monalisa said", user=payloads.user("monalisa", 777)
            ),
            delivery="c1",
        )

        assert "<@909>" not in last_post(threads)
