"""Opening an item with an assignee and two labels says one thing in the thread. Issue #81.

Three messages arrived where one was wanted: the metadata block, a line pinging the assignee the
block already mentions, and a line for each label the block already lists. Two facts made that
happen and neither was accounted for anywhere. The block is a real message the first time it is
posted, so it notifies everybody it names. And GitHub sends a `labeled` delivery for every label
an item was created with, beside the `opened` one, in the same second.

Driven through the container and the real worker, because both fixes are decisions about which
of several deliveries says something, and neither is visible from inside one service.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import Repository
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import Status
from shannon.services.workflow import build_item_workflow
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration

ISSUE_KEY = (f"{payloads.OWNER}/{payloads.REPO}".lower(), 12)

# What the issue in the report was created with: somebody on it and two labels.
LABELS = [{"name": "enhancement"}, {"name": "medium priority"}]


def labelled(name: str, labels: list[dict]) -> dict:
    """One of the `labeled` deliveries, which name the label that moved at the top level."""
    payload = payloads.issue_event("labeled", labels=labels)
    payload["label"] = {"name": name, "color": "d73a4a"}
    return payload


async def test_a_thread_opens_with_exactly_one_message(
    registered: Repository,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """The report, end to end. Counted off the thread rather than off `threads.posts`, because
    what somebody complained about is how many messages they had to read."""
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=4242
    )
    await db_session.commit()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await post(client, "issues", payloads.issue_event("opened", labels=LABELS), delivery="i-1")
        await post(client, "issues", labelled("enhancement", LABELS), delivery="i-2")
        await post(client, "issues", labelled("medium priority", LABELS), delivery="i-3")
        await client.drain()

    assert len(threads.created) == 1
    thread = threads.created[0]
    assert list(thread.messages.values()) == [threads.metadata_of(thread.thread_id)]
    assert "<@4242>" in threads.metadata_of(thread.thread_id), "the one message reached nobody"


async def test_a_status_line_is_still_said_after_the_command_that_set_it(
    registered: Repository,
    db_engine: AsyncEngine,
    threads: FakeThreadGateway,
    issue_event,
) -> None:
    """The trap in the fix above, and the reason only a POSTED block counts as having shown a
    reader anything.

    A status command writes the label on GitHub and re-renders the block seconds before its own
    `labeled` delivery arrives. That block is an edit, which is invisible from the channel and
    notifies nobody, and the command's own reply is ephemeral. So this line is the only thing
    anybody else in the thread ever sees, and a gate that counted edits would have swallowed it.

    `IN_REVIEW` rather than `DONE` because an issue is only done once it is closed on GitHub,
    and the workflow refuses to say otherwise. Every status goes down the same path.
    """
    github = FakeGitHubClient(issues={ISSUE_KEY: issue_event("opened")})
    container = build_stack(db_engine, threads=threads, github=github)
    client = build_http_client(container)
    workflow = build_item_workflow(
        container.sessionmaker,
        github,
        threads,
        pr_sync=container.pr_sync,
        issue_sync=container.issue_sync,
    )

    async with client:
        await post(client, "issues", payloads.issue_event("opened"), delivery="i-1")
        await client.drain()

        await workflow.set_status(thread_id=threads.created[0].thread_id, status=Status.IN_REVIEW)

        await post(
            client,
            "issues",
            labelled("IN_REVIEW", [{"name": "bug"}, {"name": "IN_REVIEW"}]),
            delivery="i-2",
        )
        await client.drain()

    assert [body for _, body in threads.posts] == ["📋 **Status set:** `IN_REVIEW`"]
