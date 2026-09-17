from __future__ import annotations

import logging
from dataclasses import dataclass

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.commands.link import build_link_command
from shannon.commands.link_team import build_link_team_command
from shannon.commands.mentions import build_mentions_command
from shannon.commands.refresh import build_refresh_command
from shannon.commands.register import build_register_command
from shannon.commands.set_channel import build_set_channel_command
from shannon.commands.sync_link import build_issue_command, build_pr_command
from shannon.commands.workflow import build_workflow_commands
from shannon.config import Settings, get_settings
from shannon.db.session import build_engine, build_sessionmaker
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.formatting import (
    format_assignee_ping,
    format_comment,
    format_commit,
    format_commits_left,
    format_force_push,
    format_label_change,
    format_review,
    format_reviewer_ping,
    format_state_change,
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
from shannon.services.mentions import MentionPreferences
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.projects import ProjectPoller
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.sync.announcements import AnnouncesInThread, Arrival
from shannon.services.sync.commit_lines import CommitLine
from shannon.services.sync.items import (
    ItemSyncService,
    Notifier,
    build_item_handler,
    build_item_sync,
)
from shannon.services.sync.label_lines import LabelLine
from shannon.services.sync.manual import build_issue_sync, build_pull_request_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import (
    IssuePolicy,
    PullRequestPolicy,
    TicketPolicy,
    channel_fallbacks,
)
from shannon.services.sync.refresh import RepositoryRefresh
from shannon.services.sync.relocation import Mirror, ThreadRelocation
from shannon.services.sync.shutting import KeepsThreadsShut
from shannon.services.sync.state_lines import StateLine
from shannon.services.workflow import ItemWorkflow, build_item_workflow

logger = logging.getLogger(__name__)


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

    async def forget_channel(self, channel_id: int) -> None:
        """Let go of every thread that was in a channel Discord says has gone.

        The companion to `forget_thread`, for the deletions Discord does not report one by one.
        It reports a thread deleted with its channel only while discord.py still has that thread
        cached, and it drops one the moment the thread archives, so the quiet threads are
        announced by nothing. A draft card parked in a column nobody touches is exactly that, and
        it has no webhook to rebuild it either.

        Silent about a channel holding nothing of ours, which is most of the ones Discord reports.
        """
        async with self.sessionmaker() as session, session.begin():
            forgotten = await ThreadPointerStore(session).forget_channel(channel_id)
        if forgotten:
            logger.info(
                "channel %s was deleted, so %s tracked items let go of their threads",
                channel_id,
                len(forgotten),
            )

    async def forget_thread(self, thread_id: int) -> None:
        """Let go of a thread somebody deleted in Discord.

        Handed to the client so the gateway can say so, rather than every path finding out by
        being refused. A draft card never finds out at all: nothing but the poller visits one,
        and the poller decides from timestamps and a stored pointer without asking Discord, so a
        card parked in a column nobody touches again is mirrored nowhere for good.

        Silent about a thread that is not one of ours, which is most of the ones Discord reports.
        """
        async with self.sessionmaker() as session, session.begin():
            item = await TrackedItemStore(session).get_by_thread(thread_id)
            if item is None:
                return
            await ThreadPointerStore(session).forget_thread(item.id, dead_thread_id=thread_id)
            tracked_item_id = item.id
        logger.info(
            "thread %s was deleted, so tracked item %s lets go of it", thread_id, tracked_item_id
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
        self, *, tracked_item_id: int, thread_id: int, guild_id: int, the_block_pinged: bool
    ) -> tuple[str, ...]:
        told = await self.people.notify(
            tracked_item_id=tracked_item_id,
            thread_id=thread_id,
            guild_id=guild_id,
            the_block_pinged=the_block_pinged,
        )
        told_teams = await self.teams.notify(
            tracked_item_id=tracked_item_id,
            thread_id=thread_id,
            guild_id=guild_id,
            the_block_pinged=the_block_pinged,
        )
        return (*told, *told_teams)


def _both(people: Notifier, teams: Notifier) -> Notifier:
    return _Both(people, teams)


@dataclass(frozen=True, slots=True)
class _EveryAnnouncer:
    """Every announcer behind the one seam the item handler has for saying something.

    The same shape `_both` above gives the two notifiers, and for the same reason: which lines a
    thread gets is a wiring decision, and the handler that runs them should not grow a parameter
    for each. A third is a longer tuple here and no edit to `items.py`.

    Order is not a behaviour. `labeled`/`unlabeled` and `closed`/`reopened` are disjoint, so at
    most one of these ever has anything to say about a given delivery.
    """

    announcers: tuple[AnnouncesInThread, ...]

    async def say(self, arrival: Arrival) -> None:
        for announcer in self.announcers:
            await announcer.say(arrival)


def _every(*announcers: AnnouncesInThread) -> AnnouncesInThread:
    return _EveryAnnouncer(announcers)


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
                    sessionmaker,
                    threads,
                    role=ActorRole.REVIEWER,
                    render=format_reviewer_ping,
                    # So the line names somebody who ran `/mentions off` without ringing them.
                    muted=MutedMemberStore,
                ),
                ActorNotifier(
                    sessionmaker,
                    threads,
                    role=ActorRole.REVIEWER_TEAM,
                    render=format_team_ping,
                    mentions=TeamLinkStore,
                    # The one notifier the block does not stand in for. A team is named in the
                    # block as plain text and never looked up, so the block reaches nobody on
                    # its behalf, and silencing this beside the others would stop a team ever
                    # being told a review was asked of it.
                    the_block_pings_them=False,
                    # And no `muted=`, because this one's ids are roles rather than accounts. A
                    # member cannot opt out of a role ping at all: Discord rings everybody who
                    # holds it. Filtering them would have been harmless rather than wrong, since
                    # a snowflake is unique across entity types so no role id could be sitting in
                    # `muted_members`, and it is left off so nobody has to work that out.
                ),
            ),
        ),
        build_item_sync(
            sessionmaker,
            threads,
            IssuePolicy(),
            ActorNotifier(
                sessionmaker,
                threads,
                role=ActorRole.ASSIGNEE,
                render=format_assignee_ping,
                muted=MutedMemberStore,
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

    # Everything that posts into a thread gets one of these, because posting is what reopens a
    # thread: Discord takes no message into an archived one, so a comment on a closed issue and
    # the closing header itself both leave the thread open behind them unless it is shut again.
    shut_again = KeepsThreadsShut(sessionmaker, threads)

    comments = ItemNoteMirror(
        sessionmaker, threads, render=format_comment, rebuild=rebuild, shut_again=shut_again
    )
    reviews = ItemNoteMirror(
        sessionmaker, threads, render=format_review, rebuild=rebuild, shut_again=shut_again
    )

    router = EventRouter()
    # One of each for both kinds of item, because a label moves the same way on either and so
    # does a close. Given to the item handlers rather than to the sync service: the sync runs for
    # commands and the board as well, and neither of those has a delivery to announce.
    #
    # The commit line is last because it is the only one of the three that reads GitHub, which
    # makes it the only one that can take a delivery down for a reason outside this process. The
    # two above have already said their piece by the time it starts.
    #
    # It goes to the issue handler as well, and does nothing there: an issue has no `synchronize`
    # action, which is the first thing it checks.
    announce = _every(
        LabelLine(sessionmaker, threads, render=format_label_change, shut_again=shut_again),
        StateLine(sessionmaker, threads, render=format_state_change, shut_again=shut_again),
        CommitLine(
            sessionmaker,
            threads,
            github,
            render=format_commit,
            rewritten=format_force_push,
            left=format_commits_left,
            shut_again=shut_again,
        ),
    )
    router.register(
        "pull_request", build_item_handler(pr_sync, parse_pull_request_event, announce=announce)
    )
    router.register("issues", build_item_handler(issue_sync, parse_issue_event, announce=announce))
    router.register("issue_comment", build_note_handler(comments, parse_comment_event))
    router.register(
        "pull_request_review",
        build_note_handler(
            reviews, parse_review_event, then=ReviewRequestLedger(sessionmaker).fulfilled
        ),
    )
    return router


def _refresh(
    sessionmaker: async_sessionmaker, github: GitHubClient, threads: ThreadGateway
) -> RepositoryRefresh:
    """The refresh path's own sync services, built with no notifier.

    A refresh mirrors a backlog, and everybody on it was asked when their item was opened, often
    months ago. Built without a notifier rather than told not to ping: there is nothing to fire,
    so no later edit to the sync path can make one fire, and nothing about being silent is a rule
    somebody has to keep reading.

    Two more services is two more constructor calls. Each holds a sessionmaker, a stateless lock,
    the one thread gateway, a stateless binding and a stateless policy: no connection, no task,
    no cache. The poller already pays this for tickets, one line down.
    """
    return RepositoryRefresh(
        sessionmaker,
        github,
        # Mentions off, because every thread this opens is a FIRST one and a first block is
        # posted. Twenty-five of them in a run would notify everybody on all twenty-five about a
        # backlog that has been sitting there.
        pull_requests=build_item_sync(sessionmaker, threads, PullRequestPolicy(), mentions=False),
        issues=build_item_sync(sessionmaker, threads, IssuePolicy(), mentions=False),
    )


def _relocation(
    sessionmaker: async_sessionmaker, github: GitHubClient, threads: ThreadGateway
) -> ThreadRelocation:
    """The relocation path's own sync services: built to relocate, and built without a notifier.

    Both halves are properties of the object rather than arguments to a call, for the same reason
    the refresh path's are. A binding that cannot relocate cannot be talked into it by a later
    edit, which is what keeps a webhook delivery from moving a thread and leaving the old one open
    with nothing said in it; and a service with no notifier cannot ping a backlog of reviewers
    because their threads were rehoused.

    Tickets are deliberately absent. A draft board card has no endpoint to fetch it by number, so
    it takes the other route: its pointer is let go of and the poller opens the replacement.
    """
    return ThreadRelocation(
        sessionmaker,
        threads,
        mirrors={
            # Built with mentions, and it still pings nobody. Every thread this opens replaces
            # one, and a replacement gets the block with the people named in plain text; the
            # threads it only rewrites are edits, which notify nobody either way. Turning them
            # off here as well would give one outcome two owners, and a later edit could undo
            # the one that matters while the tests went on passing on the other.
            ObjectType.PR: Mirror(
                service=build_item_sync(sessionmaker, threads, PullRequestPolicy(), relocates=True),
                fetch=github.get_pull_request,
            ),
            ObjectType.ISSUE: Mirror(
                service=build_item_sync(sessionmaker, threads, IssuePolicy(), relocates=True),
                fetch=github.get_issue,
            ),
        },
    )


def _commands(
    sessionmaker: async_sessionmaker,
    github: GitHubClient,
    gate: PermissionGate,
    workflow: ItemWorkflow,
    pr_sync: ItemSyncService,
    issue_sync: ItemSyncService,
    refresh: RepositoryRefresh,
    relocation: ThreadRelocation,
) -> tuple[app_commands.Command, ...]:
    """Every slash command the bot installs.

    A command missing from here is one that silently stops existing in Discord, so the tuple is
    built once at wiring time rather than assembled on demand.
    """
    return (
        build_register_command(RepositoryRegistrationService(sessionmaker, github), gate),
        build_set_channel_command(
            ChannelMappingService(sessionmaker, channel_fallbacks()), relocation, gate
        ),
        build_pr_command(build_pull_request_sync(sessionmaker, github, pr_sync), gate),
        build_issue_command(build_issue_sync(sessionmaker, github, issue_sync), gate),
        build_refresh_command(refresh, gate),
        build_link_command(UserLinkingService(sessionmaker, github), gate),
        build_link_team_command(TeamLinkingService(sessionmaker), gate),
        # The only one here with no gate, which is visible at a glance and is the point. See
        # `_permissions.UNGATED`.
        build_mentions_command(MentionPreferences(sessionmaker)),
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
            may_set_status=settings.board_may_set_status,
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
            _refresh(sessionmaker, github, threads),
            _relocation(sessionmaker, github, threads),
        ),
    )
