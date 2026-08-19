from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from shannon.discord_bot.formatting import (
    format_comment,
    format_issue,
    format_pull_request,
    thread_name,
)
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT, MESSAGE_LIMIT
from shannon.discord_bot.threads import THREAD_NAME_LIMIT, truncate_thread_name
from shannon.domain.enums import Priority, Status
from shannon.domain.errors import UnparseableLinkError
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.domain.priority import parse_priority
from shannon.domain.time import as_utc
from shannon.github import mapping
from shannon.github.mapping import parse_timestamp
from shannon.github.urls import parse_issue_url, parse_pull_request_url
from shannon.github.webhooks.comments import parse_comment_event
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.sync.staleness import is_superseded

REPO = RepositorySnapshot(github_repo_id=1, owner="o", name="n", html_url="https://github.com/o/n")

text = st.text(max_size=200)
logins = st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=40)
actors = st.builds(Actor, login=logins)
labels = st.builds(Label, name=text)
aware = st.datetimes(timezones=st.just(UTC))


class TestPriority:
    @given(st.lists(text, max_size=8))
    def test_it_always_answers_with_a_priority(self, names: list[str]) -> None:
        assert parse_priority(names) in set(Priority)

    @given(st.lists(text, max_size=8))
    def test_order_does_not_change_the_answer(self, names: list[str]) -> None:
        """Labels arrive in whatever order GitHub feels like."""
        assert parse_priority(names) is parse_priority(list(reversed(names)))

    @given(st.lists(text, max_size=6), st.lists(text, max_size=6))
    def test_adding_labels_never_lowers_the_priority(
        self, first: list[str], second: list[str]
    ) -> None:
        rank = {Priority.UNSET: 0, Priority.LOW: 1, Priority.MEDIUM: 2, Priority.HIGH: 3}
        assert rank[parse_priority(first + second)] >= min(
            rank[parse_priority(first)], rank[parse_priority(second)]
        )


class TestLinkParsing:
    @given(st.integers(min_value=1, max_value=10**9))
    def test_a_generated_pull_request_link_round_trips(self, number: int) -> None:
        ref = parse_pull_request_url(f"https://github.com/some-owner/some.repo/pull/{number}")

        assert ref.owner == "some-owner"
        assert ref.name == "some.repo"
        assert ref.number == number

    @given(st.integers(min_value=1, max_value=10**9))
    def test_a_generated_issue_link_round_trips(self, number: int) -> None:
        ref = parse_issue_url(f"https://github.com/some-owner/some.repo/issues/{number}")

        assert ref.number == number

    @given(text)
    @settings(max_examples=400)
    def test_it_never_raises_anything_but_its_own_error(self, link: str) -> None:
        """Whatever someone types after `/pr`, they should get an answer rather than a crash."""

        for parse in (parse_pull_request_url, parse_issue_url):
            with contextlib.suppress(UnparseableLinkError):
                parse(link)


class TestTimestamps:
    @given(st.one_of(text, st.none(), st.integers(), st.booleans()))
    def test_parsing_never_raises(self, value: object) -> None:
        parsed = parse_timestamp(value)
        assert parsed is None or parsed.tzinfo is not None

    @given(aware)
    def test_a_parsed_timestamp_keeps_its_instant(self, moment: datetime) -> None:
        parsed = parse_timestamp(moment.isoformat())

        assert parsed is not None
        assert parsed.timestamp() == moment.timestamp()

    @given(st.datetimes())
    def test_as_utc_is_idempotent(self, moment: datetime) -> None:
        assert as_utc(as_utc(moment)) == as_utc(moment)


class TestStaleness:
    @given(aware, st.none())
    def test_a_missing_timestamp_is_no_evidence(self, moment: datetime, nothing: None) -> None:
        """Either side missing means there is nothing to compare, so nothing to conclude."""
        assert is_superseded(moment, nothing) is False
        assert is_superseded(nothing, moment) is False

    @given(aware)
    def test_a_snapshot_is_never_stale_against_itself(self, moment: datetime) -> None:
        assert is_superseded(moment, moment) is False

    @given(aware, aware)
    def test_exactly_one_direction_is_stale(self, a: datetime, b: datetime) -> None:
        """Two different instants: one is before the other, and only one way round."""
        forward = is_superseded(a, b)
        backward = is_superseded(b, a)

        if a == b:
            assert not forward and not backward
        else:
            assert forward != backward


class TestRendering:
    metadata = st.builds(
        IssueSnapshot,
        repository=st.just(REPO),
        github_object_id=st.integers(min_value=1),
        number=st.integers(min_value=1, max_value=10**7),
        title=text,
        html_url=text,
        state=st.sampled_from(["open", "closed", "", "OPEN"]),
        author=st.one_of(st.none(), actors),
        assignees=st.lists(actors, max_size=5).map(tuple),
        labels=st.lists(labels, max_size=5).map(tuple),
        updated_at=st.one_of(st.none(), aware),
    )

    @given(metadata, st.sampled_from(list(Status)), st.sampled_from(list(Priority)))
    @settings(max_examples=200)
    def test_an_issue_block_always_fits_discord(
        self, snapshot: IssueSnapshot, status: Status, priority: Priority
    ) -> None:
        assert len(format_issue(snapshot, status=status, priority=priority)) <= MESSAGE_LIMIT

    @given(metadata, st.sampled_from(list(Status)))
    @settings(max_examples=200)
    def test_an_issue_block_never_loses_a_field(
        self, snapshot: IssueSnapshot, status: Status
    ) -> None:
        """Truncation must not silently drop the fields at the bottom of the block."""
        rendered = format_issue(snapshot, status=status)

        if len(rendered) < MESSAGE_LIMIT:
            for field in ("Issue Name", "Type", "State", "Status", "Priority", "Last Updated"):
                assert f"**{field}:**" in rendered

    @given(
        st.builds(
            PullRequestSnapshot,
            repository=st.just(REPO),
            github_object_id=st.integers(min_value=1),
            number=st.integers(min_value=1, max_value=10**7),
            title=text,
            html_url=text,
            state=st.sampled_from(["open", "closed"]),
            merged=st.booleans(),
            reviewers=st.lists(actors, max_size=5).map(tuple),
        ),
        st.sampled_from(list(Status)),
    )
    def test_a_pull_request_block_always_fits_discord(
        self, snapshot: PullRequestSnapshot, status: Status
    ) -> None:
        assert len(format_pull_request(snapshot, status=status)) <= MESSAGE_LIMIT

    @given(st.text(max_size=4000), st.one_of(st.none(), aware))
    @settings(max_examples=200)
    def test_a_comment_always_fits_discord(self, body: str, when: datetime | None) -> None:
        snapshot = CommentSnapshot(
            repository=REPO,
            item_number=1,
            comment_id=1,
            html_url="https://github.com/o/n/issues/1#issuecomment-1",
            body=body,
            author=Actor("octocat"),
            created_at=when,
        )

        rendered = format_comment(snapshot)
        assert len(rendered) <= MESSAGE_LIMIT
        assert "**octocat** commented" in rendered

    @given(st.text(min_size=COMMENT_PREVIEW_LIMIT + 1, max_size=4000))
    def test_a_long_comment_body_is_always_cut(self, body: str) -> None:
        snapshot = CommentSnapshot(
            repository=REPO,
            item_number=1,
            comment_id=1,
            html_url="",
            body=body,
            author=Actor("octocat"),
        )

        assert len(format_comment(snapshot)) <= MESSAGE_LIMIT


class TestThreadNames:
    @given(text)
    def test_a_thread_name_always_fits_discord(self, title: str) -> None:
        snapshot = IssueSnapshot(
            repository=REPO,
            github_object_id=1,
            number=1,
            title=title,
            html_url="",
            state="open",
        )

        assert len(truncate_thread_name(thread_name(snapshot))) <= THREAD_NAME_LIMIT

    @given(text)
    def test_a_thread_name_is_never_empty(self, title: str) -> None:
        """Discord rejects a blank thread name."""
        assert truncate_thread_name(title).strip() != ""


json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=30),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=12), children, max_size=6),
    ),
    max_leaves=15,
)
json_objects = st.dictionaries(st.text(max_size=12), json_values, max_size=8)


class TestParsersAgainstArbitraryPayloads:
    """Webhook bodies come off the network. A parser that raises takes the request down with it.

    Every one of these should answer with a snapshot or with nothing, whatever it is handed.
    """

    @given(st.text(max_size=20), json_objects)
    @settings(max_examples=300)
    def test_the_pull_request_parser_never_raises(self, action: str, payload: dict) -> None:

        parse_pull_request_event(action, payload)

    @given(st.text(max_size=20), json_objects)
    @settings(max_examples=300)
    def test_the_issue_parser_never_raises(self, action: str, payload: dict) -> None:

        parse_issue_event(action, payload)

    @given(st.text(max_size=20), json_objects)
    @settings(max_examples=300)
    def test_the_comment_parser_never_raises(self, action: str, payload: dict) -> None:

        parse_comment_event(action, payload)

    @given(st.text(max_size=20), json_objects)
    @settings(max_examples=300)
    def test_the_review_parser_never_raises(self, action: str, payload: dict) -> None:

        parse_review_event(action, payload)

    @given(json_values)
    @settings(max_examples=300)
    def test_the_field_mappers_never_raise(self, value: object) -> None:

        assert mapping.actor(value) is None or isinstance(mapping.actor(value), Actor)
        assert isinstance(mapping.actors(value), tuple)
        assert isinstance(mapping.labels(value), tuple)
        assert isinstance(mapping.is_pull_request(value), bool)
        mapping.repository(value)
        mapping.issue(value, REPO)
        mapping.pull_request(value, REPO)
