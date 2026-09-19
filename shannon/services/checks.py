"""What CI made of a pull request, said in its thread, to the people it concerns. Issue #112.

Beside the note mirror rather than under `sync/`, because it is neither. It does not sync an item
and it carries no `Arrival`: a check suite is its own delivery, so it resolves its own thread the
way `ItemNoteMirror` does rather than being handed one.

The guards are ordered so the cheapest refusal comes first. A suite with nothing to say about it,
which is most of them on a repository that runs CI on every branch, costs a parse and one indexed
read and never touches GitHub.

Nothing here reads `item_assignments.notified_at`. That column answers whether somebody has been
told they are ON an item, once, for the life of the row; a CI ping recurs, and the same reviewer is
rung again the next time CI finishes. Using it would also fire once over every `AUTHOR` row written
since `PullRequestPolicy.assignments` shipped, none of which has ever been claimed, which is the
incident migration `0021` exists to prevent. The claim in `mirrored_notes` is what makes this say
a thing once, and it needs no migration at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import Notify, PostsToThread
from shannon.domain.enums import ObjectType
from shannon.domain.errors import ItemNotReadyError
from shannon.domain.json import JsonObject
from shannon.domain.models import Actor, CheckReport, CheckRun, PullRequestSnapshot
from shannon.github.webhooks.checks import CheckSuiteEvent
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.sync.announcements import ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)

# What GitHub says a run is doing once it has stopped. Tested against rather than against a set of
# pending words on purpose: GitHub has added `waiting`, `requested` and `pending` since this
# endpoint was written, and a guard listing the ones it knew about reads a new one as finished and
# announces a result while jobs are still running.
FINISHED = "completed"


class ReadsChecksAndItems(Protocol):
    """The two reads a check result needs, and nothing that could write anything.

    Its own protocol rather than the whole client, on the grounds the rest of this project uses:
    this runs on every completed suite, which is every push to every branch running CI, and a
    handle that could also move a label is one that could move a label by accident there.
    """

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...


class Renders(Protocol):
    """Turning a report and an audience into the message.

    Injected rather than imported, so this module never reaches `formatting` and the renderer it
    is given is the only thing that decides what a reader sees.
    """

    def __call__(
        self,
        report: CheckReport,
        *,
        people: Sequence[Actor],
        teams: Sequence[Actor],
        mentions: Mapping[str, int] | None,
        roles: Mapping[str, int] | None,
    ) -> Panel: ...


Parses = Callable[[str, JsonObject], CheckSuiteEvent | None]


class CheckSuiteAnnouncer:
    """Says what CI did, once per set of results, to whoever it is about."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        github: ReadsChecksAndItems,
        *,
        render: Renders,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._line = ClaimedLine(sessionmaker, threads, shut_again)
        self._github = github
        self._render = render
        self._said_it_is_working = False

    async def announce(self, event: CheckSuiteEvent) -> bool:
        """Report this suite on every pull request it heads. True if anything was said."""
        self._note_that_checks_are_arriving()
        said = False
        for number in event.numbers:
            said = await self._one(event, number) or said
        return said

    def _note_that_checks_are_arriving(self) -> None:
        """Say once that a check suite reached this process at all.

        `Checks: Read` is a permission added to an App that is already installed, and granting one
        suspends event delivery until somebody accepts the change. Until they do, no suite arrives
        and the feature is indistinguishable from a broken one. This is the positive signal: a
        line in the log the first time one lands, so "did the permission go through" is a question
        with an answer.
        """
        if self._said_it_is_working:
            return
        self._said_it_is_working = True
        logger.info("check suites are reaching this bot, so the Checks permission is granted")

    async def _one(self, event: CheckSuiteEvent, number: int) -> bool:
        owner, name = event.repository.owner, event.repository.name
        found = await self._locate(event, number)
        if found is None:
            return False
        tracked_item_id, thread_id, guild_id = found

        item = await self._github.get_pull_request(owner, name, number)
        if item.head_sha != event.head_sha:
            # Superseded. `cancel-in-progress` stops the previous run the moment a new commit is
            # pushed, and it completes as `cancelled` with its finished jobs still reading
            # `success`, so it looks like a partial failure of work nobody is looking at any more.
            # The suite's own conclusion cannot be used for this: GitHub takes the worst of its
            # runs and `cancelled` outranks `failure`, so that test would swallow a real break.
            logger.info(
                "%s#%s has moved off %s, so its checks are not announced",
                event.repository.full_name,
                number,
                event.head_sha,
            )
            return False
        if item.closed:
            # Posting reopens an archived thread and something then has to shut it again. A
            # merged pull request does not want a late CI result either way.
            logger.info(
                "%s#%s is closed, so its checks are not announced",
                event.repository.full_name,
                number,
            )
            return False

        report = await self._report(event, owner, name)
        if report is None:
            return False

        return await self._say(event, item, report, tracked_item_id, thread_id, guild_id)

    async def _locate(self, event: CheckSuiteEvent, number: int) -> tuple[int, int, int] | None:
        """The item, its thread and its server, in one read.

        The guild comes off the repository row this already had to fetch, rather than a second
        query for it. `ItemNoteMirror._find_thread` reads it the same way and for the same reason.
        """
        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_github_id(
                event.repository.github_repo_id
            )
            if repository is None:
                logger.info(
                    "a check suite arrived for %s, which is not registered to any guild",
                    event.repository.full_name,
                )
                return None

            item = await TrackedItemStore(session).get_by_number(
                repository_id=repository.id, number=number, object_type=ObjectType.PR
            )
            if item is None:
                logger.info(
                    "checks on %s#%s are not tracked here, ignoring",
                    event.repository.full_name,
                    number,
                )
                return None

            if item.discord_thread_id is None:
                # Retried rather than dropped. A suite can finish while the `opened` delivery that
                # builds the thread is still behind a Discord outage, and answering "nothing to
                # do" loses the result for good because nothing revisits that.
                raise ItemNotReadyError(f"{event.repository.full_name}#{number} has no thread yet")

            return item.id, item.discord_thread_id, repository.discord_guild_id

    async def _report(self, event: CheckSuiteEvent, owner: str, name: str) -> CheckReport | None:
        """Every check on the commit, or None if there is nothing worth saying about them.

        The whole commit rather than the suite that reported it. A suite belongs to one app, so a
        repository running GitHub Actions beside anything else has several finishing at different
        moments, and each would otherwise report a fraction of the answer as the whole of it.
        """
        runs = await self._github.list_check_runs(owner, name, event.head_sha)
        if not runs:
            logger.info("GitHub listed no checks on %s, so nothing is said", event.head_sha)
            return None

        pending = [run.name for run in runs if run.status != FINISHED]
        if pending:
            logger.info(
                "%s still has %s running, so its checks are not announced yet",
                event.head_sha,
                ", ".join(sorted(pending)),
            )
            return None

        report = CheckReport(sha=event.head_sha, runs=tuple(runs))
        if not report.worth_saying:
            # Nothing ran. A path filter skipped every job on a docs-only push, and narrating that
            # is noise in a thread nobody asked to have narrated.
            logger.info("nothing ran on %s, so nothing is said about it", event.head_sha)
            return None
        return report

    async def _say(
        self,
        event: CheckSuiteEvent,
        item: PullRequestSnapshot,
        report: CheckReport,
        tracked_item_id: int,
        thread_id: int,
        guild_id: int,
    ) -> bool:
        people, teams = _who_to_tell(item, report)
        mentions, roles, notify = await self._resolve(guild_id, people, teams)
        await self._line.say_once(
            tracked_item_id=tracked_item_id,
            thread_id=thread_id,
            note_key=report.note_key,
            panel=self._render(report, people=people, teams=teams, mentions=mentions, roles=roles),
            notify=notify,
        )
        logger.info(
            "checks on %s#%s: %s of %s passed",
            event.repository.full_name,
            item.number,
            len(report.succeeded),
            report.total,
        )
        return True

    async def _resolve(
        self, guild_id: int, people: tuple[Actor, ...], teams: tuple[Actor, ...]
    ) -> tuple[dict[str, int], dict[str, int], Notify]:
        """Names into mentions, and the allow-list that decides which of them ring.

        The ids go with the logins rather than the names alone, because that is the only evidence
        of identity on this path: GitHub frees a login when an account is renamed or deleted, and
        without the id a mention meant for one person reaches whoever took the name.

        Roles are deliberately absent from the allow-list. `_may_notify` only ever sets `users`,
        so a role ping is governed by the client's own rule and a member cannot opt out of one at
        all: Discord rings everybody holding the role. That asymmetry is already written down
        beside the team notifier in the container, and it is restated here rather than left to be
        rediscovered.
        """
        async with self._sessionmaker() as session:
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=guild_id,
                people={person.login: person.github_user_id for person in people},
            )
            roles = await TeamLinkStore(session).resolve_many(
                guild_id=guild_id, people=dict.fromkeys((team.login for team in teams), None)
            )
            notify = await MutedMemberStore(session).may_be_pinged(
                guild_id=guild_id, ids=mentions.values()
            )
        return dict(mentions), dict(roles), notify


def _who_to_tell(
    item: PullRequestSnapshot, report: CheckReport
) -> tuple[tuple[Actor, ...], tuple[Actor, ...]]:
    """Who this result is for.

    A draft rings nobody. GitHub runs CI on one like any other pull request, and reviewers have
    not been asked to look at it yet. The results are still posted, because the person writing it
    is the one who wants the failures.

    A pass goes to the reviewers, a failure to the author and whoever is assigned. Deduplicated,
    because assigning yourself your own pull request is the ordinary thing to do.
    """
    if item.draft:
        return (), ()
    if report.passed:
        return tuple(item.reviewers), tuple(item.reviewer_teams)
    author = (item.author,) if item.author else ()
    people = {person.login.lower(): person for person in (*author, *item.assignees)}
    return tuple(people.values()), ()


def build_check_suite_handler(announcer: CheckSuiteAnnouncer, parse: Parses) -> EventHandler:
    """The seam the router registers, shaped like the other two handler builders.

    `arrived` is taken and ignored. Every other handler keys its claim on the delivery number; a
    check result keys on the set of runs it read, because two suites on one commit are two
    deliveries that must produce one message between them.
    """

    async def handle(
        action: str, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        event = parse(action, payload)
        if event is None:
            return WebhookOutcome.IGNORED
        return (
            WebhookOutcome.PROCESSED if await announcer.announce(event) else WebhookOutcome.IGNORED
        )

    return handle
