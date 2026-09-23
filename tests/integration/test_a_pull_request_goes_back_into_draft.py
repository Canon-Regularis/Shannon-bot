"""A pull request put back into draft, which until issue #140 was deliberately silent.

Issue #132 listed `converted_to_draft` so the card could be repainted grey and said nothing out
loud about it, on the grounds that a line announcing it would ring the very people it was
withdrawing the ask from. That objection was real and is answered here rather than ignored: the
people are rung, because somebody reviewing a pull request that has gone back into draft is
spending time on work that is not asking for it, and a team is named in plain text, because a
role mention reaches everybody holding the role and Discord gives nobody a way to leave one
person out of one.

Driven end to end, like the file for the other half: the action has to survive the endpoint's own
filter, reach the queue, and come back out with a delivery number.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote, Repository
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot import formatting
from shannon.discord_bot.formatting import format_back_to_draft
from shannon.discord_bot.threads import Notify
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.draft_lines import DRAFTED, DraftSwitchLine
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()

# GitHub stamps `updated_at` when the switch is thrown, so each delivery here is newer than the
# one before it.
READY_AT = "2026-08-10T12:05:00Z"
DRAFTED_AT = "2026-08-10T12:10:00Z"
LATER_STILL = "2026-08-10T12:15:00Z"

MONALISA = 555
HUBOT = 606
OCTOCAT = 111
ROLE = 777_000

ACCOUNTS = {"octocat": 583231, "monalisa": 200, "hubot": 100, "bigboss": 900}

# Read off the renderer rather than restated, so a pasted emoji that differs by a variation
# selector cannot leave every assertion below passing on an empty list.
HEADING = formatting._DRAFTED_HEADING


async def link_account(session: AsyncSession, login: str, discord_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1,
        github_username=login,
        github_user_id=ACCOUNTS[login],
        discord_user_id=discord_id,
    )
    await session.commit()


def drafted(sender: str = "octocat", **overrides: Any) -> dict[str, Any]:
    """A `converted_to_draft` body, with whoever pressed the button on it."""
    overrides.setdefault("updated_at", DRAFTED_AT)
    payload = payloads.pull_request_event("converted_to_draft", draft=True, **overrides)
    payload["sender"] = payloads.user(sender, ACCOUNTS[sender])
    return payload


def ready_again(**overrides: Any) -> dict[str, Any]:
    overrides.setdefault("updated_at", LATER_STILL)
    payload = payloads.pull_request_event("ready_for_review", draft=False, **overrides)
    payload["sender"] = payloads.user("octocat", ACCOUNTS["octocat"])
    return payload


def lines(threads: FakeThreadGateway) -> list[tuple[str, Notify]]:
    """Every back-to-draft line posted, beside what each was allowed to ring."""
    return [
        (content, notify)
        for kind, _, content, notify in threads.allowed
        if kind == "post" and content.startswith(HEADING)
    ]


def said(threads: FakeThreadGateway) -> list[str]:
    return [content for content, _ in lines(threads)]


def handler(sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway):
    """The announcer on its own, for the tests about when it speaks rather than to whom."""
    return build_item_handler(
        build_item_sync(sessionmaker, threads, PullRequestPolicy()),
        parse_pull_request_event,
        announce=DraftSwitchLine(
            sessionmaker,
            threads,
            half=DRAFTED,
            render=format_back_to_draft,
            shut_again=KeepsThreadsShut(sessionmaker, threads),
        ),
    )


async def opened_and_ready(http_client: DeliveryClient) -> None:
    """Where these start: a pull request open, not a draft, and waiting on somebody."""
    await deliver(
        http_client,
        "pull_request",
        payloads.pull_request_event("opened", draft=False),
        delivery="p0",
    )


class TestItSaysSo:
    async def test_going_back_into_draft_posts_a_line(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The inversion of what issue #132 decided. The card going grey was the only sign, and a
        card is an edit, which Discord announces to nobody."""
        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(http_client, "pull_request", drafted(sender="bigboss"), delivery="p1")

        assert said(threads) == [
            f"{HEADING}\nmonalisa hubot octocat **bigboss** converted this pull request to draft."
        ]

    async def test_the_author_and_the_assignees_are_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """What issue #140 asked for by name."""
        await link_account(db_session, "octocat", OCTOCAT)
        await link_account(db_session, "hubot", HUBOT)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(
                http_client,
                "pull_request",
                drafted(sender="bigboss", requested_reviewers=[]),
                delivery="p1",
            )

        assert lines(threads) == [
            (
                f"{HEADING}\n<@{HUBOT}> <@{OCTOCAT}> "
                "**bigboss** converted this pull request to draft.",
                (OCTOCAT, HUBOT),
            )
        ]

    async def test_the_reviewers_are_told_too(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Past what the issue asked for, deliberately. The reviewers are the people who were
        asked to spend time on it, so they are the ones with something to stop doing."""
        await link_account(db_session, "monalisa", MONALISA)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(
                http_client,
                "pull_request",
                drafted(sender="bigboss", assignees=[]),
                delivery="p1",
            )

        content, notify = lines(threads)[0]
        assert f"<@{MONALISA}>" in content
        assert notify is not None and MONALISA in notify

    async def test_whoever_pressed_it_is_not_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """They know: they pressed it. The ordinary case, since an author usually drafts their
        own work back."""
        await link_account(db_session, "octocat", OCTOCAT)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(
                http_client,
                "pull_request",
                drafted(requested_reviewers=[], assignees=[]),
                delivery="p1",
            )

        assert lines(threads) == [
            (f"{HEADING}\n**octocat** converted this pull request to draft.", ())
        ]

    async def test_a_linked_team_is_named_but_its_role_is_not_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The decision this half turns on, against a real row rather than an empty mapping: with
        nothing linked it would pass for the wrong reason.

        Discord rings everybody holding a role and offers no way to leave one person out, so a
        role mention here would wake a whole team to tell them to stop. The thread still records
        that the team was asked.
        """
        await TeamLinkStore(db_session).link(
            guild_id=1, github_team="backend", discord_role_id=ROLE
        )
        await db_session.commit()

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(
                http_client,
                "pull_request",
                drafted(
                    sender="bigboss",
                    requested_reviewers=[],
                    requested_teams=[{"slug": "backend"}],
                    assignees=[],
                ),
                delivery="p1",
            )

        content, notify = lines(threads)[0]
        assert "backend" in content
        assert "<@&" not in content, "a role mention would ring the whole team"
        assert notify == ()

    async def test_a_muted_member_is_named_but_not_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await link_account(db_session, "monalisa", MONALISA)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=MONALISA)
        await db_session.commit()

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_and_ready(http_client)
            await deliver(
                http_client,
                "pull_request",
                drafted(sender="bigboss", assignees=[]),
                delivery="p1",
            )

        content, notify = lines(threads)[0]
        assert f"<@{MONALISA}>" in content
        assert notify == ()


class TestWhenItIsSaid:
    async def test_a_pull_request_opened_as_a_draft_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The obvious way to get this wrong. Opening a draft withdraws nothing, because nothing
        was ever asked."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened", draft=True), 900_001)

        assert said(threads) == []

    async def test_one_delivery_says_it_once(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """And under its own key, which is what stops the two halves claiming each other's."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)
        await handle("opened", payloads.pull_request_event("opened", draft=False), 900_001)
        went_back = drafted()

        await handle("converted_to_draft", went_back, 900_002)
        await handle("converted_to_draft", went_back, 900_002)

        assert len(said(threads)) == 1
        held = await db_session.scalar(
            select(func.count())
            .select_from(MirroredNote)
            .where(MirroredNote.note_key == "draft:900002")
        )
        assert held == 1, "the claim that makes it say it once was not taken"

    async def test_a_second_trip_into_draft_says_it_again(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Keyed on the delivery, so each withdrawal is its own. The ask was made again in
        between, and withdrawn again after it."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened", draft=False), 900_001)
        await handle("converted_to_draft", drafted(), 900_002)
        await handle("ready_for_review", ready_again(), 900_003)
        await handle("converted_to_draft", drafted(updated_at="2026-08-10T12:20:00Z"), 900_004)

        assert len(said(threads)) == 2

    async def test_a_delivery_overtaken_by_a_return_to_ready_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A late stand-down is false in the way a late summons is, and it is false loudly: it
        would tell the reviewers to stop looking at a pull request that is asking again."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened", draft=False), 900_001)
        await handle("ready_for_review", ready_again(), 900_003)
        # The one GitHub sent first, held up behind a back-off while the one above went through.
        await handle("converted_to_draft", drafted(), 900_002)

        assert said(threads) == []
