"""A pull request leaving draft, which until issue #132 this bot never heard about.

`ready_for_review` was not a supported action, so the delivery was turned away at the endpoint and
nothing behind it ever ran. Two things followed. The card kept the grey it was painted as a draft
until some unrelated delivery happened along, so a pull request that went ready and then sat quiet
read as a draft in the channel indefinitely. And nobody was told, which is what the issue reports:
a draft rings nobody on purpose, and nothing was watching for the moment that stops being true.

Driven end to end rather than against the sync directly, because half of what makes this work is
outside it. The action has to survive the endpoint's own filter, reach the queue, and come back out
of it with a delivery number. A test calling the sync with a hand-built snapshot would prove none
of that, and the filter is exactly what was missing.
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
from shannon.discord_bot.formatting import format_ready_for_review
from shannon.discord_bot.panels import Accent, Panel
from shannon.discord_bot.threads import Notify
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.announcements import Arrival
from shannon.services.sync.draft_lines import READY, DraftSwitchLine
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

# GitHub stamps `updated_at` when the draft switch is thrown, so each delivery here is newer than
# the one before it. Equal stamps would be ordered by arrival and work too; this is what arrives.
READY_AT = "2026-08-10T12:05:00Z"
DRAFTED_AT = "2026-08-10T12:10:00Z"
LATER_STILL = "2026-08-10T12:15:00Z"

# The Discord accounts behind the logins below, where a test has run /link for them.
MONALISA = 555
HUBOT = 606
ROLE = 777_000

# GitHub's ids for the same people. Carried because `resolve_many` drops a link whose stored id
# disagrees with the one on the item, so a test that made them up would resolve nobody.
ACCOUNTS = {"octocat": 583231, "monalisa": 200, "hubot": 100, "bigboss": 900}

# Read off the renderer rather than restated. A pasted emoji that differs by a variation selector
# looks identical here and matches nothing, so every assertion below would pass on an empty list.
HEADING = formatting._READY_HEADING


async def link_account(session: AsyncSession, login: str, discord_id: int) -> None:
    """What /link leaves behind. The GitHub id goes with it, because `resolve_many` drops a link
    whose stored id disagrees with the one on the item and a made-up one resolves nobody."""
    await UserLinkStore(session).link(
        guild_id=1,
        github_username=login,
        github_user_id=ACCOUNTS[login],
        discord_user_id=discord_id,
    )
    await session.commit()


def ready(sender: str = "octocat", **overrides: Any) -> dict[str, Any]:
    """A `ready_for_review` body, with whoever pressed the button on it.

    The sender is settable because the payload helper fixes it at the author, and the author is
    exactly the case that proves nothing: a line naming the initiator and a line naming the author
    read identically until those are two different people.
    """
    overrides.setdefault("updated_at", READY_AT)
    payload = payloads.pull_request_event("ready_for_review", draft=False, **overrides)
    payload["sender"] = payloads.user(sender, ACCOUNTS[sender])
    return payload


def lines(threads: FakeThreadGateway) -> list[tuple[str, Notify]]:
    """Every ready line posted, beside what each was allowed to ring.

    The allow-list is kept because it is half the answer and the other half cannot show it: a
    member who has run `/mentions off` is named in the text exactly as somebody who has not.
    """
    return [
        (content, notify)
        for kind, _, content, notify in threads.allowed
        if kind == "post" and content.startswith(HEADING)
    ]


def said(threads: FakeThreadGateway) -> list[str]:
    return [content for content, _ in lines(threads)]


def handler(sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway):
    """The announcer on its own, for the tests about when it speaks rather than to whom.

    Directly rather than through the endpoint because these drive one delivery twice, or two
    deliveries out of order, and the endpoint answers a repeated delivery id before any of that
    can happen.
    """
    return build_item_handler(
        build_item_sync(sessionmaker, threads, PullRequestPolicy()),
        parse_pull_request_event,
        announce=DraftSwitchLine(
            sessionmaker,
            threads,
            half=READY,
            render=format_ready_for_review,
            shut_again=KeepsThreadsShut(sessionmaker, threads),
        ),
    )


def card(threads: FakeThreadGateway) -> Panel:
    """The metadata block as it was last written, which is the card the channel shows.

    Picked out of the panels by what only the block carries. Taking the last panel written would
    read whatever line was posted after it, and the point of this file is that there is one.
    """
    blocks = [panel for panel in threads.panels if "**State:**" in panel.text]
    assert blocks, "no metadata block was ever written"
    return blocks[-1]


async def opened_as_a_draft(http_client: DeliveryClient) -> None:
    """Where every test here starts: a thread open on a pull request nobody is meant to read yet."""
    await deliver(
        http_client,
        "pull_request",
        payloads.pull_request_event("opened", draft=True),
        delivery="p0",
    )


class TestTheCardCatchesUp:
    """The half of issue #132 that the action being listed at all is enough to fix.

    Worth its own class because it is a separate failure from the silence: it is visible to
    anybody scrolling the channel, it needs no ping to reproduce, and it was there for every
    pull request that ever left draft.
    """

    async def test_a_draft_is_grey(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The colour this starts from, so the test below reads as a change rather than a
        coincidence about what `Accent.OPEN` happens to be."""
        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)

        assert card(threads).accent is Accent.DRAFT

    async def test_being_marked_ready_repaints_it(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                payloads.pull_request_event("ready_for_review", draft=False, updated_at=READY_AT),
                delivery="p1",
            )

        assert card(threads).accent is Accent.OPEN

    async def test_going_back_to_draft_greys_it_again(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Why both halves of the switch are listed rather than the one the issue names. With only
        `ready_for_review`, a pull request taken back to draft would keep the green it was last
        told about and read as ready for review until something else moved."""
        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                payloads.pull_request_event("ready_for_review", draft=False, updated_at=READY_AT),
                delivery="p1",
            )
            await deliver(
                http_client,
                "pull_request",
                payloads.pull_request_event(
                    "converted_to_draft", draft=True, updated_at=DRAFTED_AT
                ),
                delivery="p2",
            )

        assert card(threads).accent is Accent.DRAFT


class TestWhoIsTold:
    """The audience is read off the pull request itself rather than off the ledger.

    `item_assignments.notified_at` answers once for the life of a row, and a reviewer asked while
    this was still a draft has already spent theirs, so a notifier here would tell nobody at all.
    That is the whole reason this is an announcer.
    """

    async def test_the_reviewers_and_the_assignees_are_both_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Together, in one message. Two would be two notifications for one event, and the second
        would carry no information the first did not."""
        await link_account(db_session, "monalisa", MONALISA)
        await link_account(db_session, "hubot", HUBOT)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(http_client, "pull_request", ready(), delivery="p1")

        assert lines(threads) == [
            (
                f"{HEADING}\n<@{MONALISA}> <@{HUBOT}> **octocat** marked this pull request "
                "ready for review.",
                # Sorted, not in the order the sentence names them: `may_be_pinged` answers who
                # this bot may ring, and an allow-list has no reading order to preserve.
                (MONALISA, HUBOT),
            )
        ]

    async def test_somebody_on_both_lists_is_named_once(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """GitHub keeps the two lists apart and a maintainer reviewing their own team's work is
        routinely on both. Named twice, the sentence reads as two people."""
        await link_account(db_session, "monalisa", MONALISA)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(
                    requested_reviewers=[payloads.user("monalisa", ACCOUNTS["monalisa"])],
                    assignees=[payloads.user("monalisa", ACCOUNTS["monalisa"])],
                ),
                delivery="p1",
            )

        assert said(threads) == [
            f"{HEADING}\n<@{MONALISA}> **octocat** marked this pull request ready for review."
        ]

    async def test_whoever_pressed_the_button_is_not_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """They know: they pressed it. Dropped by who acted rather than by who wrote the pull
        request, which is the distinction the whole rule turns on.

        Both halves are visible here at once. `monalisa` pressed it and is the only reviewer, so
        she is named and not rung; `octocat` wrote it, never touched it, and is told. Nobody had
        to assign him for that to happen, which is what issue #139 changed.
        """
        await link_account(db_session, "monalisa", MONALISA)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(
                    sender="monalisa",
                    requested_reviewers=[payloads.user("monalisa", ACCOUNTS["monalisa"])],
                    assignees=[],
                ),
                delivery="p1",
            )

        assert lines(threads) == [
            (f"{HEADING}\noctocat **monalisa** marked this pull request ready for review.", ())
        ]

    async def test_an_author_who_did_not_press_it_is_still_told(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The case the initiator rule exists for, and the one an author rule would get backwards:
        `bigboss` marks `octocat`'s pull request ready, and `octocat` is who wants telling.

        Nobody is assigned and nobody is asked to review, so the author is the only person the
        line can reach. Before issue #139 this test passed by planting the author in `assignees`,
        which proved the initiator rule and nothing whatever about authors.
        """
        await link_account(db_session, "octocat", 111)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(sender="bigboss", requested_reviewers=[], assignees=[]),
                delivery="p1",
            )

        assert lines(threads) == [
            (f"{HEADING}\n<@111> **bigboss** marked this pull request ready for review.", (111,))
        ]

    async def test_an_author_who_is_also_assigned_is_named_once(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The author reaches the set by two roads now, and a person named twice in one sentence
        reads as two people. Deduped on the login, like everybody else."""
        await link_account(db_session, "octocat", 111)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(
                    sender="bigboss",
                    requested_reviewers=[],
                    assignees=[payloads.user("octocat", ACCOUNTS["octocat"])],
                ),
                delivery="p1",
            )

        assert lines(threads) == [
            (f"{HEADING}\n<@111> **bigboss** marked this pull request ready for review.", (111,))
        ]

    async def test_a_pull_request_whose_author_has_gone_still_names_the_rest(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """GitHub sends a null `user` for a deleted account, the same way it does for a sender.
        The reviewers are still waiting on it whoever opened it."""
        await link_account(db_session, "monalisa", MONALISA)

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(sender="bigboss", user=None, assignees=[]),
                delivery="p1",
            )

        assert lines(threads) == [
            (
                f"{HEADING}\n<@{MONALISA}> **bigboss** marked this pull request ready for review.",
                (MONALISA,),
            )
        ]

    async def test_a_muted_member_is_named_but_not_rung(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """`/mentions off`. The text is what delivers a notification and the allow-list is what
        permits one, so the mention stays and the permission goes."""
        await link_account(db_session, "monalisa", MONALISA)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=MONALISA)
        await db_session.commit()

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(http_client, "pull_request", ready(assignees=[]), delivery="p1")

        assert lines(threads) == [
            (
                f"{HEADING}\n<@{MONALISA}> **octocat** marked this pull request ready for review.",
                (),
            )
        ]

    async def test_a_linked_team_is_a_role_mention(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """And never reaches the allow-list. Discord rings everybody holding a role and gives
        nobody a way to leave one person out of one, which `/mentions` already says out loud."""
        await TeamLinkStore(db_session).link(
            guild_id=1, github_team="backend", discord_role_id=ROLE
        )
        await db_session.commit()

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(
                    requested_reviewers=[],
                    requested_teams=[{"slug": "backend"}],
                    assignees=[],
                ),
                delivery="p1",
            )

        assert lines(threads) == [
            (
                f"{HEADING}\n<@&{ROLE}> **octocat** marked this pull request ready for review.",
                (),
            )
        ]

    async def test_a_pull_request_with_nobody_on_it_still_says_it(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The thread is the record. A pull request going ready with no reviewers yet is exactly
        when somebody wants to notice it and put themselves on it."""
        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(
                http_client,
                "pull_request",
                ready(requested_reviewers=[], assignees=[]),
                delivery="p1",
            )

        assert said(threads) == [
            f"{HEADING}\n**octocat** marked this pull request ready for review."
        ]

    async def test_an_account_that_has_gone_still_gets_the_line_said(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """GitHub sends a null sender for a deleted account. The pull request is ready either
        way, and the people on it are owed the ask whoever made it."""
        payload = ready(assignees=[])
        payload["sender"] = None

        async with registered_stack(db_engine, db_session, threads) as http_client:
            await opened_as_a_draft(http_client)
            await deliver(http_client, "pull_request", payload, delivery="p1")

        assert said(threads) == [
            f"{HEADING}\nmonalisa octocat **Unknown** marked this pull request ready for review."
        ]


class TestWhenItIsSaid:
    async def test_the_ready_half_says_nothing_about_going_back_into_draft(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A claim about this half's gate, not about the bot: the other action has had a line of
        its own since issue #140, and it is posted by a second instance wired beside this one.

        Worth keeping and worth the careful name. `said()` here filters on the ready heading, so
        this test cannot see the other half's line and would go on passing whatever it said. It
        proves that one instance answers one action, which is what makes two of them safe.
        """
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await handle(
            "converted_to_draft",
            payloads.pull_request_event("converted_to_draft", draft=True, updated_at=READY_AT),
            900_002,
        )

        assert said(threads) == []

    async def test_a_pull_request_opened_ready_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The obvious way to get this wrong. Most pull requests are opened ready and never see
        a draft at all, and the block that opens their thread is a real message that already
        reaches everybody it names."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened", draft=False), 900_001)

        assert said(threads) == []

    async def test_one_delivery_says_it_once(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The queue is at-least-once by design: a delivery whose status could not be written
        comes back when its lease runs out and is handled again from the top. Twice, this rings
        everybody on the pull request twice for one press of one button."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)
        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        marked = ready()

        await handle("ready_for_review", marked, 900_002)
        await handle("ready_for_review", marked, 900_002)

        assert len(said(threads)) == 1
        held = await db_session.scalar(
            select(func.count())
            .select_from(MirroredNote)
            .where(MirroredNote.note_key == "ready:900002")
        )
        assert held == 1, "the claim that makes it say it once was not taken"

    async def test_a_second_trip_through_draft_says_it_again(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Keyed on the delivery rather than on the item, and that is the right answer here: the
        ask was withdrawn and then made again, and the second one is as real as the first."""
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await handle("ready_for_review", ready(), 900_002)
        await handle(
            "converted_to_draft",
            payloads.pull_request_event("converted_to_draft", draft=True, updated_at=DRAFTED_AT),
            900_003,
        )
        await handle("ready_for_review", ready(updated_at=LATER_STILL), 900_004)

        assert len(said(threads)) == 2

    async def test_a_delivery_overtaken_by_a_return_to_draft_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """What separates this from the tag line, which posts on a superseded delivery on purpose.
        A tag line reports something that happened and stays true however late it is read. This
        one says "go and look at this now", and says it by ringing people, so arriving after the
        pull request is a draft again makes it a false summons rather than a stale note.
        """
        threads = FakeThreadGateway()
        handle = handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await handle(
            "converted_to_draft",
            payloads.pull_request_event("converted_to_draft", draft=True, updated_at=DRAFTED_AT),
            900_003,
        )
        # The one GitHub sent first, held up behind a back-off while the one above went through.
        await handle("ready_for_review", ready(), 900_002)

        assert said(threads) == []


class TestWhatItIsNotFor:
    """Both gates, driven directly, because neither is reachable through the endpoint.

    That is the point of them rather than an excuse: one tuple of announcers serves the issues
    handler as well as the pull request one, so this is handed deliveries it has no business
    speaking about, and the reason it says nothing must not be that GitHub never sends them.
    """

    async def test_an_issue_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        threads = FakeThreadGateway()
        snapshot = parse_issue_event("opened", payloads.issue_event("opened"))
        assert snapshot is not None

        await DraftSwitchLine(
            db_sessionmaker,
            threads,
            half=READY,
            render=format_ready_for_review,
            shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        ).say(
            Arrival(
                action="ready_for_review",
                snapshot=snapshot,
                payload={},
                tracked_item_id=1,
                thread_id=1,
                arrived=900_001,
            )
        )

        assert said(threads) == []

    async def test_an_item_that_has_gone_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Unreachable, since the delivery only found a thread by way of the row. Guarded anyway,
        because the cost of being wrong is an attribute read on None inside a Discord call."""
        threads = FakeThreadGateway()
        snapshot = parse_pull_request_event("ready_for_review", ready())
        assert snapshot is not None

        await DraftSwitchLine(
            db_sessionmaker,
            threads,
            half=READY,
            render=format_ready_for_review,
            shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        ).say(
            Arrival(
                action="ready_for_review",
                snapshot=snapshot,
                payload=ready(),
                tracked_item_id=9_999,
                thread_id=1,
                arrived=900_001,
            )
        )

        assert said(threads) == []
