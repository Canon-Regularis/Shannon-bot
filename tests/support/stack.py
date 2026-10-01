from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.db.models import WebhookEvent
from shannon.domain.enums import DeliveryStatus, ObjectType
from shannon.github.client import GitHubClient
from shannon.services.delivery.worker import DeliveryWorker
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support.db import map_channel, register_repository
from tests.support.signing import SECRET, post

# How many times `drain` will empty the queue and then pull parked deliveries forward before giving
# up. A round is "everything due, then unpark whatever is left", so three covers the shape this
# exists for: a first attempt, a retry, and one more for a delivery whose retry only became possible
# because another delivery's retry went first. Small enough that a handler failing every time fails
# the test in three attempts rather than after a minute of doubling backoffs.
_DRAIN_ROUNDS = 3


def build_stack(
    engine: AsyncEngine,
    *,
    threads: FakeThreadGateway | None = None,
    github: GitHubClient | None = None,
) -> Container:
    """The real container with Discord and GitHub swapped for fakes.

    Everything else is production code: the same router, the same sync service, the same
    signature check, the same database.
    """
    return build_container(
        threads=threads or FakeThreadGateway(),
        settings=Settings(github_webhook_secret=SECRET),
        engine=engine,
        github=github or FakeGitHubClient(),
    )


class DeliveryClient:
    """The endpoint and the worker behind it, driven as one.

    The endpoint only writes a delivery down, so a test that posts one and then looks at Discord
    has to run the worker in between. Doing that here keeps it out of every test.
    """

    def __init__(
        self,
        app_client: AsyncClient,
        worker: DeliveryWorker,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self.http = app_client
        self.worker = worker
        self._sessionmaker = sessionmaker

    async def __aenter__(self) -> DeliveryClient:
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.__aexit__(*exc)

    async def post(self, *args: Any, **kwargs: Any) -> Response:
        return await self.http.post(*args, **kwargs)

    async def drain(self, *, expect_retries: bool = False) -> None:
        """Work through the queue until there is genuinely nothing left, and say so if there is.

        This used to be `while await self.worker.run_once(): pass`, which is not the same thing.
        `run_once` answers how many deliveries were DUE, and a handler that raised is parked
        five seconds into the future by `retry_later` - so the loop saw nothing due, returned,
        and `deliver`'s promise of "the whole path in one call" quietly went unkept. The test
        then read a thread with no note in it and failed on whatever it asserted about the
        content, which is three steps from the cause.

        So two changes. Parked deliveries are pulled forward and tried again, because a test has
        no real clock and waiting out a backoff means nothing here. And anything still unfinished
        when the passes run out is raised, naming the delivery and the error it recorded, because
        the alternative is what happened: a failure three steps from its cause, once in thirty
        files, on a machine under load.

        `expect_retries` is for the tests that park a delivery ON PURPOSE and assert on the
        attempt count. They want the queue left exactly as the worker left it.
        """
        for _ in range(_DRAIN_ROUNDS):
            # Everything due, then the question. Checking after one batch rather than after the
            # queue is empty is how the first version of this raised on work it had just finished:
            # the last pass succeeded, the loop ran out, and nothing looked again.
            while await self.worker.run_once():
                pass
            unfinished = await self._unfinished()
            if expect_retries or not unfinished:
                return
            await self._pull_retries_forward()

        raise AssertionError(
            "drain gave up with deliveries unfinished after "
            f"{_DRAIN_ROUNDS} rounds: {await self._unfinished()}. A handler is raising every "
            "time rather than transiently; the error above is the one it recorded."
        )

    async def _unfinished(self) -> list[str]:
        """Every delivery still in a live state, described well enough to act on."""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(
                        WebhookEvent.github_delivery_id,
                        WebhookEvent.event_type,
                        WebhookEvent.status,
                        WebhookEvent.attempts,
                        WebhookEvent.last_error,
                    ).where(WebhookEvent.status.in_(DeliveryStatus.live()))
                )
            ).all()
        return [
            f"{delivery} ({event}) is {status.value} after {attempts} "
            f"attempt{'' if attempts == 1 else 's'}: {error or 'no error recorded'}"
            for delivery, event, status, attempts, error in rows
        ]

    async def _pull_retries_forward(self) -> None:
        """Make every parked delivery due now.

        The lease takes `next_attempt_at IS NULL OR next_attempt_at <= now()`, so clearing the
        column is what a clock moving forward would have done. Nothing here shortens the real
        backoff: production waits, and this is the test standing in for the wait.
        """
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.status.in_(DeliveryStatus.live()))
                .values(next_attempt_at=None)
            )

    async def outcome_of(self, delivery: str) -> str:
        """What the worker made of a delivery.

        The response no longer carries this. The endpoint answers before anything has been
        tried, so whether there was work to do is only known once the worker has run.
        """
        async with self._sessionmaker() as session:
            status = await session.scalar(
                select(WebhookEvent.status).where(WebhookEvent.github_delivery_id == delivery)
            )
        return str(status).lower() if status is not None else "not queued"

    async def attempts_of(self, delivery: str) -> int:
        """How many times the worker has tried a delivery.

        The difference between a handler that answered "nothing to do" and one that raised so the
        delivery would come back. Both leave nothing in Discord, and only this tells them apart.
        """
        async with self._sessionmaker() as session:
            attempts = await session.scalar(
                select(WebhookEvent.attempts).where(WebhookEvent.github_delivery_id == delivery)
            )
        return int(attempts) if attempts is not None else 0


@asynccontextmanager
async def registered_stack(
    engine: AsyncEngine,
    session: AsyncSession,
    threads: FakeThreadGateway,
    *,
    issues_channel: int | None = 98,
    github: GitHubClient | None = None,
) -> AsyncIterator[DeliveryClient]:
    """A registered repository with the whole stack over it, ready to take a delivery.

    Six files were building this by hand and differed only in whether issues had a channel and
    what had already been delivered. Those differences stay in the fixtures that care; the four
    lines they all repeated are here.

    `issues_channel=None` is the guild where nobody ran /set_channel for issues, which is the
    case the channel fallback exists for and must stay reachable.

    `github` is for the paths that READ an item back rather than taking it off the webhook. An
    approving review is one: deciding whether everybody has approved means asking GitHub for the
    pull request, and a fake that has never heard of it answers 404. That used to be invisible,
    because the note is posted before the check and the delivery was quietly parked afterwards -
    the visible assertion passed and the delivery never finished.
    """
    repository = await register_repository(session, guild_id=1, channel_id=99)
    if issues_channel is not None:
        await map_channel(session, repository, ObjectType.ISSUE, channel_id=issues_channel)
    async with build_http_client(build_stack(engine, threads=threads, github=github)) as client:
        yield client


def build_http_client(container: Container) -> DeliveryClient:
    app = create_app(
        settings=container.settings,
        event_router=container.event_router,
        queue=container.queue,
    )
    return DeliveryClient(
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        container.worker,
        container.sessionmaker,
    )


async def deliver(
    client: DeliveryClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
    expect_retries: bool = False,
) -> Response:
    """Post a webhook and let the worker act on it, which is the whole path in one call.

    It says that and now means it. `drain` used to stop as soon as nothing was DUE, so a handler
    that raised left its delivery parked in the future and this returned having done nothing -
    silently, to a test about to assert on what Discord was told.

    `expect_retries` is for a test that means to leave one parked and assert on the attempt
    count.
    """
    response = await post(client, event, payload, delivery=delivery)
    await client.drain(expect_retries=expect_retries)
    return response
