from __future__ import annotations

import logging
from dataclasses import dataclass

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.config import Settings, get_settings
from shannon.db.session import build_engine, build_sessionmaker
from shannon.discord_bot.commands.link import build_link_command
from shannon.discord_bot.commands.register import build_register_command
from shannon.discord_bot.commands.set_channel import build_set_channel_command
from shannon.discord_bot.commands.sync_link import build_issue_command, build_pr_command
from shannon.discord_bot.formatting import (
    format_assignee_ping,
    format_comment,
    format_review,
    format_reviewer_ping,
)
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.github.webhooks.comments import parse_comment_event
from shannon.github.webhooks.events import EventRouter
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.channels import ChannelMappingService
from shannon.services.delivery_queue import WebhookDeliveryQueue
from shannon.services.item_sync import ItemSyncService, build_item_handler
from shannon.services.linking import UserLinkingService
from shannon.services.manual_sync import ManualSync, build_issue_sync, build_pull_request_sync
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.notifications import ActorNotifier
from shannon.services.policies import IssuePolicy, PullRequestPolicy
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.worker import DeliveryWorker, WorkerSettings

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
    queue: WebhookDeliveryQueue
    worker: DeliveryWorker
    registration: RepositoryRegistrationService
    linking: UserLinkingService
    channels: ChannelMappingService
    pr_sync: ItemSyncService
    issue_sync: ItemSyncService
    comments: ItemNoteMirror
    reviews: ItemNoteMirror
    manual_sync: ManualSync
    manual_issue_sync: ManualSync
    event_router: EventRouter

    def commands(self) -> tuple[app_commands.Command, ...]:
        return (
            build_register_command(self.registration, self.gate),
            build_set_channel_command(self.channels, self.gate),
            build_pr_command(self.manual_sync, self.gate),
            build_issue_command(self.manual_issue_sync, self.gate),
            build_link_command(self.linking, self.gate),
        )

    async def aclose(self) -> None:
        """Close what was opened.

        Engine disposal sits in the finally: an HTTP client that throws on the way out must not
        take the database pool with it. `aclose` is looked up because GitHubClient does not
        declare it, and fakes standing in for the real client have nothing to close.
        """
        closer = getattr(self.github, "aclose", None)
        try:
            if closer is not None:
                await closer()
        finally:
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

    pr_sync = ItemSyncService(
        sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping),
    )
    issue_sync = ItemSyncService(
        sessionmaker,
        threads,
        IssuePolicy(),
        ActorNotifier(sessionmaker, threads, role=ActorRole.ASSIGNEE, render=format_assignee_ping),
    )

    comments = ItemNoteMirror(sessionmaker, threads, render=format_comment)
    reviews = ItemNoteMirror(sessionmaker, threads, render=format_review)

    queue = WebhookDeliveryQueue(sessionmaker)

    event_router = EventRouter()
    event_router.register("pull_request", build_item_handler(pr_sync, parse_pull_request_event))
    event_router.register("issues", build_item_handler(issue_sync, parse_issue_event))
    event_router.register("issue_comment", build_note_handler(comments, parse_comment_event))
    event_router.register(
        "pull_request_review",
        build_note_handler(
            reviews, parse_review_event, then=ReviewRequestLedger(sessionmaker).fulfilled
        ),
    )

    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        github=github,
        threads=threads,
        gate=PermissionGate(settings),
        queue=queue,
        worker=DeliveryWorker(queue, event_router, WorkerSettings.from_settings(settings)),
        registration=RepositoryRegistrationService(sessionmaker, github),
        linking=UserLinkingService(sessionmaker),
        channels=ChannelMappingService(sessionmaker),
        pr_sync=pr_sync,
        issue_sync=issue_sync,
        comments=comments,
        reviews=reviews,
        manual_sync=build_pull_request_sync(sessionmaker, github, pr_sync),
        manual_issue_sync=build_issue_sync(sessionmaker, github, issue_sync),
        event_router=event_router,
    )
