"""A name typed into a GitHub comment still reaches a muted member as a mention, and not as a ping.

Issue #80. This is the half of the feature somebody is most likely to argue about, because a
person deliberately typing `@you` is not the same kind of noise as a bot announcing a label. The
decision taken is that it counts too: the issue asks for the GitHub to Discord messages to stop
notifying, and a mirrored comment is one of those. The name is still swapped for a mention, so the
thread reads the way the comment does and clicking it still finds them.

Through the whole stack, for the reason the neighbouring file gives: the half that decides which
names to look up lives in the note path and the half that swaps them lives in the renderer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration

ALICE = 909
BOB = 808
CAROL = 707
ROLE = 777000


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        yield http_client


async def link(session: AsyncSession, login: str, *, account: int, discord: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord
    )
    await session.commit()


async def mute(session: AsyncSession, discord_user_id: int) -> None:
    await MutedMemberStore(session).mute(guild_id=1, discord_user_id=discord_user_id)
    await session.commit()


def last_note(threads: FakeThreadGateway) -> tuple[str, tuple]:
    """The note this delivery posted, refusing to read anything else as one.

    The assertion is the whole value of this helper and it earned its keep the hard way. Every
    way a note silently fails to be posted - a claim already spent, an item not tracked here, a
    delivery id already seen, a handler that raised - ends with the fixture's own `create` as the
    last write. Read straight through, that becomes a failure about the CONTENT of a message
    nobody wrote, which is three steps from the cause. `drain` now raises before this is reached
    for the last of those four; this names the other three.
    """
    kind, _, content, notify = threads.allowed[-1]
    assert kind == "post", (
        f"the last write was a {kind}, so no note was posted at all. Something declined "
        "silently: look for 'already in its thread', 'is not tracked here' or 'already seen' "
        "in the log above."
    )
    return content, notify


async def test_a_name_typed_in_a_comment_is_a_mention_that_does_not_notify(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await link(db_session, "hubot", account=100, discord=ALICE)
    await mute(db_session, ALICE)

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="can you look at this, @hubot?"),
        delivery="c1",
    )

    content, notify = last_note(threads)
    assert f"<@{ALICE}>" in content, "it fell back to plain text instead of a silent mention"
    assert notify == ()


async def test_the_author_of_a_comment_is_named_and_not_rung(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """Issue #231. The header line mentions whoever wrote the comment, off the same map as the
    body, and the allow-list leaves them out: they know what they wrote. Still a mention, so the
    thread shows who said it and clicking the name finds them - the treatment `/mentions off`
    gives a member who asked for it, given to every author for their own words."""
    await link(db_session, "monalisa", account=200, discord=BOB)

    await deliver(
        tracked, "issue_comment", payloads.issue_comment_event(body="looked at it"), delivery="c1"
    )

    content, notify = last_note(threads)
    assert f"<@{BOB}>" in content
    assert notify == ()


async def test_an_author_naming_themselves_is_not_rung_either(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """Left out by the Discord account rather than by where the mention sits, so `@monalisa`
    typed by monalisa reaches the same account through the body and is refused there too."""
    await link(db_session, "monalisa", account=200, discord=BOB)

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="as @monalisa said last week"),
        delivery="c1",
    )

    content, notify = last_note(threads)
    assert content.count(f"<@{BOB}>") == 2, "the header and the body should both name her"
    assert notify == ()


async def test_somebody_else_named_in_the_same_comment_is_unaffected(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """One person going quiet must not take the other's ping with them.

    Three people in one message, one of each kind: `hubot` muted, `monalisa` the author, and
    `octocat` neither. Only the last is rung. The second used to carry this test, and since issue
    #231 an author is never rung by their own comment, so on their own they would prove nothing.
    """
    await link(db_session, "hubot", account=100, discord=ALICE)
    await link(db_session, "monalisa", account=200, discord=BOB)
    await link(db_session, "octocat", account=583231, discord=CAROL)
    await mute(db_session, ALICE)

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="@hubot and @octocat looked at this"),
        delivery="c1",
    )

    content, notify = last_note(threads)
    assert all(f"<@{account}>" in content for account in (ALICE, BOB, CAROL))
    assert notify == (CAROL,)


async def test_a_team_named_in_a_comment_still_pings_the_role(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """The limitation, from the comment side. A role mention reaches everybody holding the role
    and Discord has no way to leave one person out of one, so role ids never go near the account
    allow-list: putting them there would be a claim this bot cannot honour."""
    await TeamLinkStore(db_session).link(guild_id=1, github_team="backend", discord_role_id=ROLE)
    await link(db_session, "hubot", account=100, discord=ALICE)
    await mute(db_session, ALICE)
    await db_session.commit()

    await deliver(
        tracked,
        "issue_comment",
        payloads.issue_comment_event(body="@Canon-Regularis/backend can you look"),
        delivery="c1",
    )

    content, notify = last_note(threads)
    assert f"<@&{ROLE}>" in content
    assert ROLE not in notify, "a role id was offered as an account this bot may notify"
