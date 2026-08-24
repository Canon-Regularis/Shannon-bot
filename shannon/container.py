from __future__ import annotations

from dataclasses import dataclass

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.commands.link import build_link_command
from shannon.commands.link_team import build_link_team_command
from shannon.commands.register import build_register_command
from shannon.commands.set_channel import build_set_channel_command
from shannon.commands.sync_link import build_issue_command, build_pr_command
from shannon.commands.workflow import build_workflow_commands
from shannon.config import Settings, get_settings
from shannon.db.session import build_engine, build_sessionmaker
from shannon.db.stores.team_links import TeamLinkStore
from shannon.discord_bot.formatting import (
    format_assignee_ping,
    format_comment,
    format_review,
    format_reviewer_ping,
    format_team_ping,
)
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.roles import ConfiguredRoles
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.models import ItemNote
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.github.projects import HttpProjectBoards
from shannon.github.webhooks.comments import parse_comment_event
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.reviews import parse_review_event
from shannon.github.webhooks.router import EventRouter
from shannon.services.channels import ChannelMappingService
from shannon.services.delivery.queue import WebhookDeliveryQueue
from shannon.services.delivery.worker import DeliveryWorker, WorkerSettings
from shannon.services.linking import TeamLinkingService, UserLinkingService
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.projects import ProjectPoller
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.sync.items import (
    ItemSyncService,
    Notifier,
    build_item_handler,
    build_item_sync,
)
from shannon.services.sync.manual import build_issue_sync, build_pull_request_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, TicketPolicy
from shannon.services.workflow import ItemWorkflow, build_item_workflow


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
    poller: ProjectPoller
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


@dataclass(frozen=True, slots=True)
class _Both:
    """Two notifiers behind the one seam the sync path has for notifying.

    A pull request can have people and teams asked for a review, and the two are told in
    different words off different tables. Composing them here keeps `ItemSyncService` asking one
    thing one question, which is what let a second kind of reviewer be added without touching it.
    """

    people: Notifier
    teams: Notifier

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int
    ) -> tuple[str, ...]:
        told = await self.people.notify(
            tracked_item_id=tracked_item_id, thread_id=thread_id, guild_id=guild_id
        )
        told_teams = await self.teams.notify(
            tracked_item_id=tracked_item_id, thread_id=thread_id, guild_id=guild_id
        )
        return (*told, *told_teams)


def _both(people: Notifier, teams: Notifier) -> Notifier:
    return _Both(people, teams)


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
            _both(
                ActorNotifier(
                    sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
                ),
                ActorNotifier(
                    sessionmaker,
                    threads,
                    role=ActorRole.REVIEWER_TEAM,
                    render=format_team_ping,
                    mentions=TeamLinkStore,
                ),
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
    github: GitHubClient,
    pr_sync: ItemSyncService,
    issue_sync: ItemSyncService,
) -> EventRouter:
    """Which GitHub events reach which handler.

    A submitted review is the only note that means something beyond its own text, so it carries
    the ledger that closes the request it answers.
    """

    async def rebuild(note: ItemNote) -> None:
        """Read the item from GitHub and put it through the ordinary sync, which opens a thread.

        Wired in for one case: a note that finds its thread deleted. Nothing else on the note
        path can mend that, because only a sync has the channel and the metadata to build a
        thread with, and a comment is not an item event. Without this the note that discovers
        the deletion is lost, and so is every one after it until an unrelated item event happens
        to arrive.

        The only call to GitHub anywhere on the note path, and it fires when a thread has
        actually gone rather than on every comment.
        """
        owner, _, name = note.repository.full_name.partition("/")
        if note.object_type is ObjectType.PR:
            await pr_sync.sync(await github.get_pull_request(owner, name, note.item_number))
        else:
            await issue_sync.sync(await github.get_issue(owner, name, note.item_number))

    comments = ItemNoteMirror(sessionmaker, threads, render=format_comment, rebuild=rebuild)
    reviews = ItemNoteMirror(sessionmaker, threads, render=format_review, rebuild=rebuild)

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
    gate: PermissionGate,
    workflow: ItemWorkflow,
    pr_sync: ItemSyncService,
    issue_sync: ItemSyncService,
) -> tuple[app_commands.Command, ...]:
    """Every slash command the bot installs.

    A command missing from here is one that silently stops existing in Discord, so the tuple is
    built once at wiring time rather than assembled on demand.
    """
    return (
        build_register_command(RepositoryRegistrationService(sessionmaker, github), gate),
        build_set_channel_command(ChannelMappingService(sessionmaker), gate),
        build_pr_command(build_pull_request_sync(sessionmaker, github, pr_sync), gate),
        build_issue_command(build_issue_sync(sessionmaker, github, issue_sync), gate),
        build_link_command(UserLinkingService(sessionmaker), gate),
        build_link_team_command(TeamLinkingService(sessionmaker), gate),
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
    workflow = build_item_workflow(
        sessionmaker, github, threads, pr_sync=pr_sync, issue_sync=issue_sync
    )
    queue = WebhookDeliveryQueue(sessionmaker)
    event_router = _event_router(sessionmaker, threads, github, pr_sync, issue_sync)

    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        github=github,
        queue=queue,
        worker=DeliveryWorker(queue, event_router, WorkerSettings.from_settings(settings)),
        poller=ProjectPoller(
            sessionmaker,
            HttpProjectBoards(github),
            build_item_sync(sessionmaker, threads, TicketPolicy()),
            workflow,
            project_number=settings.github_project_number,
            interval=settings.project_poll_seconds,
        ),
        event_router=event_router,
        pr_sync=pr_sync,
        issue_sync=issue_sync,
        commands=_commands(
            sessionmaker,
            github,
            PermissionGate(ConfiguredRoles.from_settings(settings)),
            workflow,
            pr_sync,
            issue_sync,
        ),
    )
