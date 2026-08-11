from __future__ import annotations

import logging
from dataclasses import dataclass

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.config import Settings, get_settings
from shannon.db.session import build_engine, build_sessionmaker
from shannon.discord_bot.commands.link import build_link_command
from shannon.discord_bot.commands.pr import build_pr_command
from shannon.discord_bot.commands.register import build_register_command
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.threads import ThreadGateway
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.github.webhooks.events import EventRouter
from shannon.services.idempotency import WebhookIdempotencyGuard
from shannon.services.linking import UserLinkingService
from shannon.services.manual_sync import ManualPullRequestSync
from shannon.services.notifications import ReviewerNotifier
from shannon.services.pr_sync import PullRequestSyncService, build_pull_request_handler
from shannon.services.registration import RepositoryRegistrationService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    """Everything the application is made of, assembled in one place.

    Nothing below this module constructs its own collaborators, which is what makes each piece
    swappable in tests.
    """

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    github: GitHubClient
    threads: ThreadGateway
    gate: PermissionGate
    delivery_guard: WebhookIdempotencyGuard
    registration: RepositoryRegistrationService
    linking: UserLinkingService
    pr_sync: PullRequestSyncService
    manual_sync: ManualPullRequestSync
    event_router: EventRouter

    def commands(self) -> tuple[app_commands.Command, ...]:
        return (
            build_register_command(self.registration, self.gate),
            build_pr_command(self.manual_sync, self.gate),
            build_link_command(self.linking, self.gate),
        )

    async def aclose(self) -> None:
        closer = getattr(self.github, "aclose", None)
        if closer is not None:
            await closer()
        await self.engine.dispose()


def build_container(
    *,
    threads: ThreadGateway,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    github: GitHubClient | None = None,
) -> Container:
    """Wire the application.

    `threads` is a required argument because the real gateway needs a live Discord client,
    which has to be constructed before anything that talks through it.
    """
    settings = settings or get_settings()
    engine = engine or build_engine(settings.database_url.get_secret_value())
    sessionmaker = build_sessionmaker(engine)

    github = github or HttpGitHubClient(
        token=settings.github_token.get_secret_value(),
        base_url=settings.github_api_url,
        timeout=settings.github_timeout_seconds,
    )

    notifier = ReviewerNotifier(sessionmaker, threads)
    pr_sync = PullRequestSyncService(sessionmaker, threads, notifier)

    event_router = EventRouter()
    event_router.register("pull_request", build_pull_request_handler(pr_sync))

    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        github=github,
        threads=threads,
        gate=PermissionGate(settings),
        delivery_guard=WebhookIdempotencyGuard(sessionmaker),
        registration=RepositoryRegistrationService(sessionmaker, github),
        linking=UserLinkingService(sessionmaker),
        pr_sync=pr_sync,
        manual_sync=ManualPullRequestSync(sessionmaker, github, pr_sync),
        event_router=event_router,
    )
