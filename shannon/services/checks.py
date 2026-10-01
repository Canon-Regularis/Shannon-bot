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

from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import ObjectType
from shannon.domain.json import JsonObject
from shannon.domain.models import (
    Actor,
    CheckReport,
    CheckRun,
    CommitRef,
    PullRequestSnapshot,
)
from shannon.github.webhooks.checks import CheckSuiteEvent
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.audience import everyone_who_worked_on_it, reachable
from shannon.services.locating import ItemInThread, in_its_thread
from shannon.services.sync.announcements import ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)

# GitHub keeps adding pending states (`waiting`, `requested`, `pending`), so a guard listing the
# pending words it knew about would read a new one as finished and announce mid-run.
FINISHED = "completed"


class ReadsChecksAndItems(Protocol):
    """The three reads a check result needs, and nothing that could write anything.

    The third is the commits, and it sits here rather than behind a protocol of its own because
    one object answers all three: a second handle on it would buy a name and nothing else.
    """

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...

    async def list_pull_request_commits(
        self, owner: str, name: str, number: int
    ) -> Sequence[CommitRef] | None: ...


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

        return await self._say(event, item, report, found, number)

    async def _audience_for(self, item: PullRequestSnapshot, number: int) -> tuple[Actor, ...]:
        """Everybody this result is news to, and the one read that finds them.

        Asked AFTER the report, not before. A repository running two checks apps gets a suite from
        each, and most of those are refused for a run still pending, so the commonest delivery on
        this path must not pay for a third GitHub call - nor be able to fail on one.

        A draft rings nobody, and the reason has changed. It used to be that reviewers had not
        been asked to look yet; reviewers are no longer rung by CI at all, so that argument is
        gone. What survives it is the one this bot already applies to a draft everywhere else: a
        draft is the state in which nothing is asked of anybody, which is why its card is grey and
        why the approval round-up refuses one. Somebody iterating on a draft pushes repeatedly, and
        a ping on every red run in that loop is the cost. The panel is still posted, so the result
        is in the thread either way.
        """
        if item.draft:
            return ()

        owner, name = item.repository.owner, item.repository.name
        commits = await self._github.list_pull_request_commits(owner, name, number)
        # `None` is a pull request GitHub would not list, which narrows the audience rather than
        # losing the message. Anything worse than a 404 is raised by the client and carried out of
        # here to the retry: the claim `say_once` takes would make a narrowed audience PERMANENT
        # for this set of runs, so being late is cheaper than being quietly incomplete.
        return everyone_who_worked_on_it(item, commits or ())

    async def _locate(self, event: CheckSuiteEvent, number: int) -> ItemInThread | None:
        async with self._sessionmaker() as session:
            return await in_its_thread(
                session,
                repository=event.repository,
                number=number,
                object_type=ObjectType.PR,
                about="a check suite",
            )

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
        found: ItemInThread,
        number: int,
    ) -> bool:
        people = await self._audience_for(item, number)
        async with self._sessionmaker() as session:
            # No teams. A team is not somebody who worked on this, and a role mention rings
            # everybody holding it with no way for one of them to opt out - which is why
            # `reachable` keeps roles out of the allow-list in the first place. The renderer keeps
            # its two parameters for the shape it shares with the other audience-taking renderers,
            # the way `format_everyone_approved` does.
            audience = await reachable(session, guild_id=found.guild_id, people=people, teams=())
        await self._line.say_once(
            tracked_item_id=found.tracked_item_id,
            thread_id=found.thread_id,
            note_key=report.note_key,
            panel=self._render(
                report,
                people=people,
                teams=(),
                mentions=audience.mentions,
                roles=audience.roles,
            ),
            notify=audience.notify,
        )
        logger.info(
            "checks on %s#%s: %s of %s passed",
            event.repository.full_name,
            item.number,
            len(report.succeeded),
            report.total,
        )
        return True


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
