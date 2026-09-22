"""What CI made of a pull request, said in its thread, to the people it concerns.

`item_assignments.notified_at` is unusable here: it answers once for the life of a row while a CI
ping recurs, and reading it would fire over every unclaimed `AUTHOR` row ever written, the incident
migration `0021` exists to prevent. The claim in `mirrored_notes` is what makes this say a thing
once.
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

# GitHub keeps adding pending states (`waiting`, `requested`, `pending`), so a guard listing the
# pending words it knew about would read a new one as finished and announce mid-run.
FINISHED = "completed"


class ReadsChecksAndItems(Protocol):
    """The two reads a check result needs, and nothing that could write anything."""

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...


class Renders(Protocol):
    """Turning a report and an audience into the message."""

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


class Announces(Protocol):
    """Saying what CI did, and whether there was anything to say.

    The handler below asks only this, the way it asks only `Parses` of the parser, so the
    two halves of the seam the router registers are stated the same way.
    """

    async def announce(self, event: CheckSuiteEvent) -> bool: ...


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
        self._note_first_suite()
        said = False
        for number in event.numbers:
            said = await self._one(event, number) or said
        return said

    def _note_first_suite(self) -> None:
        """Say once that a check suite reached this process at all.

        Adding `Checks: Read` to an installed App suspends event delivery until somebody accepts
        the change, so until then no suite arrives and the feature looks broken.
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
            # Superseded. `cancel-in-progress` stops the previous run on a new push and it
            # completes as `cancelled` with its finished jobs still reading `success`. The suite's
            # own conclusion cannot stand in: GitHub takes the worst of its runs and `cancelled`
            # outranks `failure`, so that test would swallow a real break.
            logger.info(
                "%s#%s has moved off %s, so its checks are not announced",
                event.repository.full_name,
                number,
                event.head_sha,
            )
            return False
        if item.closed:
            # Posting reopens an archived thread and something then has to shut it again.
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
                # Retried rather than dropped: a suite can finish while the `opened` delivery
                # that builds the thread is still behind a Discord outage, and nothing revisits a
                # "nothing to do".
                raise ItemNotReadyError(f"{event.repository.full_name}#{number} has no thread yet")

            return item.id, item.discord_thread_id, repository.discord_guild_id

    async def _report(self, event: CheckSuiteEvent, owner: str, name: str) -> CheckReport | None:
        """Every check on the commit, or None if there is nothing worth saying about them.

        The whole commit rather than the suite that reported it: a suite belongs to one app, so a
        repository running several apps has several suites, each seeing a fraction of the answer.
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
            # Nothing ran: a path filter skipped every job, and narrating that is noise.
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

        The ids travel with the logins because GitHub frees a login when an account is renamed or
        deleted, and without the id a mention meant for one person reaches whoever took the name.
        Roles are absent from the allow-list on purpose: Discord rings everybody holding a role and
        a member cannot opt out of one.
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

    A draft rings nobody: GitHub runs CI on one like any other pull request, but reviewers have not
    been asked to look yet. The panel is still posted, for the author.
    """
    if item.draft:
        return (), ()
    if report.passed:
        return tuple(item.reviewers), tuple(item.reviewer_teams)
    author = (item.author,) if item.author else ()
    people = {person.login.lower(): person for person in (*author, *item.assignees)}
    return tuple(people.values()), ()


def build_check_suite_handler(announcer: Announces, parse: Parses) -> EventHandler:
    """The seam the router registers, shaped like the other two handler builders.

    `arrived` is taken and ignored: two suites on one commit are two deliveries that must produce
    one message between them, so the claim keys on the runs read rather than the delivery number.
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
