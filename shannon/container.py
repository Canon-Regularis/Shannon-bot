from __future__ import annotations

from dataclasses import dataclass

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.commands.link import build_link_command
from shannon.commands.register import build_register_command
from shannon.commands.set_channel import build_set_channel_command
from shannon.commands.sync_link import build_issue_command, build_pr_command
from shannon.commands.workflow import build_workflow_commands
from shannon.config import Settings, get_settings
from shannon.db.session import build_engine, build_sessionmaker
from shannon.discord_bot.formatting import (
    format_assignee_ping,
    format_comment,
    format_review,
    format_reviewer_ping,
)
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.roles import ConfiguredRoles
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.github.webhooks.comments import parse_comment_event
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.reviews import parse_review_event
from shannon.github.webhooks.router import EventRouter
from shannon.services.channels import ChannelMappingService
from shannon.services.delivery.queue import WebhookDeliveryQueue
from shannon.services.delivery.worker import DeliveryWorker, WorkerSettings
from shannon.services.linking import UserLinkingService
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.sync.items import ItemSyncService, build_item_handler, build_item_sync
from shannon.services.sync.manual import build_issue_sync, build_pull_request_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.workflow import build_item_workflow


@dataclass(slots=True)
class Container:
    """What the running process holds on to.

    Only the pieces somebody outside the wiring asks for by name. The rest of what
    `build_container` assembles stays local to it, so this does not grow a field per
    collaborator and nothing can reach past the seam to a service it was not given.
    """

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    github: GitHubClient
    queue: WebhookDeliveryQueue
    worker: DeliveryWorker
    event_router: EventRouter
    pr_sync: ItemSyncService
    issue_sync: ItemSyncService
    commands: tuple[app_commands.Command, ...]

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


def _sync_services(
    sessionmaker: async_sessionmaker, threads: ThreadGateway
) -> tuple[ItemSyncService, ItemSyncService]:
    """One service per object type, differing only in policy and who gets pinged.

    Pull requests ping the reviewers they ask for, issues ping their assignees. Everything else
    about the two paths is shared, which is what keeps them from drifting.
    """
    return (
        build_item_sync(
            sessionmaker,
            threads,
            PullRequestPolicy(),
            ActorNotifier(
                sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
        ),
        build_item_sync(
            sessionmaker,
            threads,
            IssuePolicy(),
            ActorNotifier(
                sessionmaker, threads, role=ActorRole.ASSIGNEE, render=format_assignee_ping
            ),
        ),
    )


def _event_router(
    sessionmaker: async_sessionmaker,
    threads: ThreadGateway,
    pr_sync: ItemSyncService,
    issue_sync: ItemSyncService,
) -> EventRouter:
    """Which GitHub events reach which handler.

    A submitted review is the only note that means something beyond its own text, so it carries
    the ledger that closes the request it answers.
    """
    comments = ItemNoteMirror(sessionmaker, threads, render=format_comment)
    reviews = ItemNoteMirror(sessionmaker, threads, render=format_review)

    router = EventRouter()
    router.register("pull_request", build_item_handler(pr_sync, parse_pull_request_event))
    router.register("issues", build_item_handler(issue_sync, parse_issue_event))
    router.register("issue_comment", build_note_handler(comments, parse_comment_event))
    router.register(
        "pull_request_review",
        build_note_handler(
            reviews, parse_review_event, then=ReviewRequestLedger(sessionmaker).fulfilled
        ),
    )
    return router


def _commands(
    sessionmaker: async_sessionmaker,
    github: GitHubClient,
    threads: ThreadGateway,
    gate: PermissionGate,
    pr_sync: ItemSyncService,
    issue_sync: ItemSyncService,
) -> tuple[app_commands.Command, ...]:
    """Every slash command the bot installs.

    A command missing from here is one that silently stops existing in Discord, so the tuple is
    built once at wiring time rather than assembled on demand.
    """
    workflow = build_item_workflow(
        sessionmaker, github, threads, pr_sync=pr_sync, issue_sync=issue_sync
    )
    return (
        build_register_command(RepositoryRegistrationService(sessionmaker, github), gate),
        build_set_channel_command(ChannelMappingService(sessionmaker), gate),
        build_pr_command(build_pull_request_sync(sessionmaker, github, pr_sync), gate),
        build_issue_command(build_issue_sync(sessionmaker, github, issue_sync), gate),
        build_link_command(UserLinkingService(sessionmaker), gate),
        *build_workflow_commands(workflow, gate),
    )


def build_container(
    *,
    threads: ThreadGateway,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    github: GitHubClient | None = None,
) -> Container:
    """Wire the application.

    `threads` is a required argument because the real gateway needs a live Discord client, which
    has to be constructed before anything that talks through it.
    """
    settings = settings or get_settings()
    engine = engine or build_engine(settings.database_url.get_secret_value())
    sessionmaker = build_sessionmaker(engine)
    github = github or HttpGitHubClient(
        token=settings.github_token.get_secret_value(),
        base_url=settings.github_api_url,
        timeout=settings.github_timeout_seconds,
    )

    pr_sync, issue_sync = _sync_services(sessionmaker, threads)
    queue = WebhookDeliveryQueue(sessionmaker)
    event_router = _event_router(sessionmaker, threads, pr_sync, issue_sync)

    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        github=github,
        queue=queue,
        worker=DeliveryWorker(queue, event_router, WorkerSettings.from_settings(settings)),
        event_router=event_router,
        pr_sync=pr_sync,
        issue_sync=issue_sync,
        commands=_commands(
            sessionmaker,
            github,
            threads,
            PermissionGate(ConfiguredRoles.from_settings(settings)),
            pr_sync,
            issue_sync,
        ),
    )
