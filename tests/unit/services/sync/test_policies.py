from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.panels import BlockKind
from shannon.discord_bot.safe_text import DESCRIPTION_PREVIEW_LIMIT
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.errors import PermanentError, WrongPolicyError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
    TicketSnapshot,
)
from shannon.github.mentions import MENTION_LIMIT
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, TicketPolicy

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
COMMON = {
    "repository": REPO,
    "github_object_id": 100,
    "number": 7,
    "title": "Add the webhook endpoint",
    "html_url": "https://github.com/Canon-Regularis/Shannon-bot/pull/7",
    "state": "open",
    "author": Actor("octocat"),
    "updated_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
}
PULL_REQUEST = PullRequestSnapshot(**COMMON)
ISSUE = IssueSnapshot(**COMMON)
TICKET = TicketSnapshot(
    repository=REPO,
    github_object_id=901,
    number=6,
    title="Write the poller",
    html_url="https://github.com/orgs/Canon-Regularis/projects/6",
    state="open",
    updated_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    column="In Progress",
    project_number=6,
)


class TestWhatEachPolicyShuts:
    """Three answers, and the pull request needs all three.

    True and False are the thread being shut and given back. None is the one that is easy to
    lose: it means leave the thread exactly as it is, and it is the only thing standing between
    `/set_done` and the next `synchronize` webhook undoing it.
    """

    def test_a_closed_pull_request_is_shut(self) -> None:
        closed = replace(PULL_REQUEST, state="closed")

        assert PullRequestPolicy().shut(closed, status=Status.NOT_REVIEWED) is True

    def test_a_merged_pull_request_is_shut(self) -> None:
        """Merged and abandoned are one case here, which `display_state` already folds."""
        merged = replace(PULL_REQUEST, state="closed", merged=True)

        assert PullRequestPolicy().shut(merged, status=Status.NOT_REVIEWED) is True

    def test_set_done_survives_the_next_webhook(self) -> None:
        """The trap. `/set_done` shuts an OPEN pull request's thread and the payload has no idea,
        so answering False here would give the thread back on the next `synchronize` and undo a
        command somebody ran on purpose. Only the row's status can tell it apart.
        """
        assert PullRequestPolicy().shut(PULL_REQUEST, status=Status.DONE) is None

    def test_a_reopened_pull_request_gets_its_thread_back(self) -> None:
        """The other half, and the reason None is not the answer for every open one. Nothing else
        on any path ever unshuts a pull request's thread."""
        assert PullRequestPolicy().shut(PULL_REQUEST, status=Status.NOT_REVIEWED) is False

    def test_an_open_issue_is_given_back(self) -> None:
        assert IssuePolicy().shut(ISSUE, status=Status.NOT_REVIEWED) is False

    def test_a_closed_issue_is_shut(self) -> None:
        closed = replace(ISSUE, state="closed")

        assert IssuePolicy().shut(closed, status=Status.NOT_REVIEWED) is True

    def test_an_issue_does_not_read_the_status(self) -> None:
        """It has no `/set_done` of its own: the command sends you to close it on GitHub, so the
        payload is the whole story and DONE on an open issue means nothing here."""
        assert IssuePolicy().shut(ISSUE, status=Status.DONE) is False


class TestWhatEachPolicyStores:
    def test_a_pull_request_records_author_assignees_and_reviewers(self) -> None:
        roles = PullRequestPolicy().assignments(
            replace(PULL_REQUEST, assignees=(Actor("hubot"),), reviewers=(Actor("monalisa"),))
        )

        assert [actor.login for actor in roles[ActorRole.REVIEWER]] == ["monalisa"]
        assert [actor.login for actor in roles[ActorRole.ASSIGNEE]] == ["hubot"]

    def test_an_issue_has_no_reviewers_at_all(self) -> None:
        roles = IssuePolicy().assignments(replace(ISSUE, assignees=(Actor("hubot"),)))

        assert ActorRole.REVIEWER not in roles

    def test_closing_an_issue_marks_it_done(self) -> None:
        closed = replace(ISSUE, state="closed")

        assert IssuePolicy().status_for(closed, Status.NOT_REVIEWED) is Status.DONE

    def test_closing_a_pull_request_leaves_its_status_alone(self) -> None:
        closed = replace(PULL_REQUEST, state="closed")

        assert PullRequestPolicy().status_for(closed, Status.IN_REVIEW) is Status.IN_REVIEW

    def test_the_object_type_each_one_owns(self) -> None:
        assert PullRequestPolicy().object_type is ObjectType.PR
        assert IssuePolicy().object_type is ObjectType.ISSUE


class TestAPolicyHandedTheWrongKindOfSnapshot:
    """Nothing pairs these but the wiring, and MVP 4 adds a third kind to get wrong."""

    async def test_a_pull_request_policy_refuses_an_issue(self) -> None:
        service = build_item_sync(None, None, PullRequestPolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="PullRequestPolicy was handed a ISSUE"):
            await service.sync(ISSUE)

    async def test_an_issue_policy_refuses_a_pull_request(self) -> None:
        service = build_item_sync(None, None, IssuePolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="IssuePolicy was handed a PR"):
            await service.sync(PULL_REQUEST)

    async def test_it_names_the_item_so_the_wiring_can_be_found(self) -> None:
        service = build_item_sync(None, None, IssuePolicy())  # type: ignore[arg-type]

        with pytest.raises(WrongPolicyError, match="Canon-Regularis/Shannon-bot#7"):
            await service.sync(PULL_REQUEST)

    async def test_it_is_permanent_so_the_worker_does_not_retry_a_wiring_bug(self) -> None:
        assert issubclass(WrongPolicyError, PermanentError)


class TestWhatTheRowAloneSaysAboutTheLock:
    """Asked where there is no payload to ask instead: a delivery turned away as superseded is
    refused before anything reads its snapshot, so the row is the only thing that knows whether
    the item's thread is finished with.

    Each kind answers from a different column, and the differences are the point.
    """

    def test_a_pull_request_is_finished_when_somebody_says_so_or_github_does(self) -> None:
        """`/set_done` writes the status, and closing or merging writes the state. Either shuts
        it, which is why both arms are asserted: an `or` short-circuits."""
        policy = PullRequestPolicy()

        assert policy.shut_for_state(status=Status.DONE, github_state="open") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="closed") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="merged") is True
        assert policy.shut_for_state(status=Status.IN_REVIEW, github_state="open") is False

    def test_an_issue_is_finished_when_github_closes_it(self) -> None:
        """Its state, not its status. `/set_done` can put an open issue at DONE, and an open
        issue's thread is one people are still meant to be talking in."""
        policy = IssuePolicy()

        assert policy.shut_for_state(status=Status.NOT_REVIEWED, github_state="closed") is True
        assert policy.shut_for_state(status=Status.DONE, github_state="open") is False

    @pytest.mark.parametrize("state", ["open", "archived", "deleted"])
    def test_a_ticket_is_never_finished_with(self, state: str) -> None:
        """A card in Done is a card somebody can drag back out, so a sync never locks its thread.

        Not even one whose row says its card was archived or deleted (issue #198). That lock is
        the poller's, taken and lifted beside the card's own state; a sync settling a lock it owes
        is the only thing that asks this, and no sync owes a ticket one.
        """
        policy = TicketPolicy()

        assert policy.shut_for_state(status=Status.DONE, github_state=state) is False


class EveryoneIsLinked(Mapping[str, int]):
    """A server where every name has an account, so the block swaps every name it reaches.

    The ids are handed out in the order the swap asks, which lets a test read back off the block
    which names were swapped, and compare them with the ones the policy read.
    """

    def __init__(self) -> None:
        self.asked: dict[str, int] = {}

    def __getitem__(self, login: str) -> int:
        return self.asked.setdefault(login, 1000 + len(self.asked))

    def __iter__(self) -> Iterator[str]:
        return iter(self.asked)

    def __len__(self) -> int:
        return len(self.asked)

    def __bool__(self) -> bool:
        """Never empty, though nobody has been asked about yet: the swap reads an empty map as
        no map at all, and would ask it nothing."""
        return True


def swapped(policy: PullRequestPolicy | IssuePolicy, body: str) -> tuple[str, ...]:
    """The logins the block shows as mentions under its label, in the order they are written."""
    linked = EveryoneIsLinked()
    snapshot = replace(PULL_REQUEST if isinstance(policy, PullRequestPolicy) else ISSUE, body=body)
    card = policy.render(
        snapshot, status=Status.NOT_REVIEWED, priority=Priority.UNSET, mentions=linked
    )
    description = next(part for part in card.blocks if part.kind is BlockKind.BODY)
    logins = {account: login for login, account in linked.asked.items()}
    shown = (logins[int(account)] for account in re.findall(r"<@(\d+)>", description.text))
    return tuple(dict.fromkeys(shown))


ITEMS = [
    pytest.param(PullRequestPolicy(), PULL_REQUEST, id="pull request"),
    pytest.param(IssuePolicy(), ISSUE, id="issue"),
]


class TestWhoADescriptionNames:
    """The people the block's description @names, which the sync rings when it opens. Issue #259.

    The same answer for a pull request and an issue: both blocks show the description through
    `as_description`, and both read it through it.
    """

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_each_name_once_lowercased_in_the_order_written(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """Lowercased because that is how a link is stored, and once because one person rings
        once however often they are named."""
        named = replace(item, body="Thanks @Monalisa, and @hubot too. @MONALISA, look again.")

        assert policy.names_written(named) == ("monalisa", "hubot")

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_a_team_is_not_somebody_to_ring(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """The block writes a team as text and never as a role mention, so there is nobody to
        look up, and a slug that matches a login is not that person."""
        named = replace(item, body="@canon-regularis/backend, and @monalisa")

        assert policy.names_written(named) == ("monalisa",)

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_no_more_than_ten(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """The budget a comment has. The eleventh is shown as written and is nobody's ping."""
        logins = [f"u{i}" for i in range(MENTION_LIMIT + 1)]
        named = replace(item, body=" ".join(f"@{login}" for login in logins))

        assert policy.names_written(named) == tuple(logins[:MENTION_LIMIT])

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_a_team_spends_the_budget_it_shares_with_people(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """Ten distinct names, teams among them, because the swap counts them that way. Reading
        ten people past a team would ring a tenth person the block leaves as text."""
        logins = [f"u{i}" for i in range(MENTION_LIMIT)]
        named = replace(item, body="@canon-regularis/backend " + " ".join(f"@{x}" for x in logins))

        assert policy.names_written(named) == tuple(logins[: MENTION_LIMIT - 1])

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_a_name_in_code_or_an_address_is_nobody(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """GitHub reads no mention in a code span, and the block shows one there as text."""
        body = "`@span` and\n```\n@fenced\n```\nmail monalisa@example.com, ask @hubot"

        assert policy.names_written(replace(item, body=body)) == ("hubot",)

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_a_name_past_the_preview_is_not_read(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        """Read off what the block shows, not the whole body. A name the cut went through is
        the front of somebody's name, and ringing it would ring whoever is linked by it."""
        past = replace(item, body="x" * DESCRIPTION_PREVIEW_LIMIT + " @hubot")
        through = replace(item, body="x" * (DESCRIPTION_PREVIEW_LIMIT - 16) + " @monalisa @hubot")

        assert policy.names_written(past) == ()
        assert policy.names_written(through) == ("monalisa",)

    @pytest.mark.parametrize(("policy", "item"), ITEMS)
    def test_nothing_written_names_nobody(
        self, policy: PullRequestPolicy | IssuePolicy, item: PullRequestSnapshot | IssueSnapshot
    ) -> None:
        assert policy.names_written(replace(item, body="")) == ()

    def test_a_ticket_names_nobody(self) -> None:
        """Its block swaps nothing into a draft's body, and ringing for it would start every
        card on a board at once, which is why its `assignments` answers nobody too."""
        assert TicketPolicy().names_written(replace(TICKET, body="@monalisa, @hubot")) == ()


class TestTheNamesReadAreTheNamesTheBlockSwaps:
    """The reader and the swap, through the real render, issue #259.

    A name read and not swapped rings somebody the block shows as text; a name swapped and not
    read shows as a mention that rang nobody. Neither one is reported anywhere, so each body
    below is one of the shapes where two recipes could tell the description apart.
    """

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("@Monalisa, @hubot and @MONALISA", id="case and repeats"),
            pytest.param("@canon-regularis/backend and @monalisa", id="a team"),
            pytest.param("`@span`\n```\n@fenced\n```\n@hubot", id="code"),
            pytest.param("monalisa@example.com and @hubot", id="an address"),
            pytest.param(
                "![@monalisa](https://user-images.githubusercontent.com/1/shot.png) @hubot",
                id="a lifted image's alt text",
            ),
            pytest.param("<!-- @hidden -->@monalisa", id="an html comment"),
            pytest.param(
                "x" * (DESCRIPTION_PREVIEW_LIMIT - 16) + " @monalisa @hubot", id="at the cut"
            ),
            pytest.param(" ".join(f"@u{i}" for i in range(MENTION_LIMIT + 2)), id="past ten"),
            pytest.param("see deadbeefcafe and @hubot", id="a commit hash"),
        ],
    )
    @pytest.mark.parametrize("policy", [PullRequestPolicy(), IssuePolicy()], ids=["pr", "issue"])
    def test_the_same_names_in_the_same_order(
        self, policy: PullRequestPolicy | IssuePolicy, body: str
    ) -> None:
        item = PULL_REQUEST if isinstance(policy, PullRequestPolicy) else ISSUE

        assert policy.names_written(replace(item, body=body)) == swapped(policy, body)
