"""Invariants over generated input, where a table can only name the cases somebody thought of.

Every strategy here was MEASURED before it was trusted, because not measuring them is how this
file came to assert nothing. `st.text(max_size=N)` does not produce strings near N - the mean
length of `st.text(max_size=3000)` is 7.0 and the longest of 400 draws was 210 - so every
property about a LIMIT used to be asserted two orders of magnitude below it, and every property
about a STRUCTURE never saw one: 0/300 bodies held a `<@id>` token, 0/400 links had a github.com
host, 0/300 generated dicts carried a field any mapper reads, 0/200 panels needed trimming, and
the longest thread name drawn was 35 characters against a limit of 100.

Two rules came out of fixing that, and both are load-bearing:

- A hazard is reached by CONSTRUCTION, not by luck. Where a draw has to contain something - a
  real priority label, a mention the caller supplied, a number made of non-ASCII digits - it is
  spliced in rather than hoped for. Repeating a branch inside `st.one_of` does NOT weight it:
  `one_of(x, x, x, y)` was measured at 58% x, not 75%.
- Where a claim has a half the generated space cannot reach, that half stays with the table test
  that already covers it, and the comment says which. A property that spends 300 examples on an
  early return is worse than no property, because it reads like coverage.

The numbers each strategy was accepted on are in the comment above it.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from shannon.discord_bot.formatting import (
    format_comment,
    format_issue,
    format_pull_request,
    format_reminder,
    thread_name,
)
from shannon.discord_bot.panels import PANEL_BUDGET, BlockKind
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT
from shannon.discord_bot.threads import THREAD_NAME_LIMIT, truncate_thread_name
from shannon.domain.enums import ObjectType, Priority, Status
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
from shannon.github.mentions import names_in
from shannon.github.safe_text import one_message
from shannon.github.urls import parse_issue_url, parse_pull_request_url
from shannon.services.sync.staleness import is_superseded

REPO = RepositorySnapshot(github_repo_id=1, owner="o", name="n", html_url="https://github.com/o/n")

text = st.text(max_size=200)
logins = st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=40)
actors = st.builds(Actor, login=logins)
labels = st.builds(Label, name=text)
aware = st.datetimes(timezones=st.just(UTC))

# A title long enough to fill a card on its own, which is the ONLY field that can. Measured: the
# description is cut to DESCRIPTION_PREVIEW_LIMIT before it is ever rendered, so a body of any
# size leaves the block comfortably inside the budget - 0/120 draws of `st.text(min_size=3900)`
# as the body needed trimming - and the people lines are clipped too: 0/120 draws of a 150-to-250
# strong assignee list needed it either. A long title: 120/120.
huge_titles = st.text(min_size=PANEL_BUDGET, max_size=PANEL_BUDGET + 600)


class TestPriority:
    """`parse_priority` over label names that are actually priority labels.

    The vocabulary and the spellings are `shannon/domain/priority.py`'s own, because a strategy
    that cannot produce a priority label measures nothing about reading one: `st.text` produced
    one in 2 draws out of 400, so all three of these asserted UNSET against UNSET.

    The empty list and the all-unrelated list stay with `tests/unit/domain/test_priority.py`,
    which names them. What a table cannot do is sweep arbitrary names past the reader without
    raising, and decide `max` over a list nobody chose.
    """

    # 400/400 draws contain at least one real priority. 142/400 contain more than one DISTINCT
    # priority, which is where `max` is actually decided rather than merely exercised.
    words = st.sampled_from(
        ("high", "urgent", "critical", "medium", "med", "moderate", "low", "minor")
    )
    styles = st.sampled_from(
        (
            "{w}",
            "{W}",
            "Priority: {w}",
            "priority-{w}",
            "prio/{w}",
            "p {w}",
            "P-{w}",
            "{w}_priority",
            "{W} PRIORITY",
            "  {w}  ",
        )
    )
    names = st.builds(lambda style, w: style.format(w=w, W=w.upper()), styles, words)
    # Near misses on purpose: a numbered scheme and a bare prefix are the two shapes the reader
    # deliberately refuses, and a word merely CONTAINING a priority is the third.
    near_misses = st.sampled_from(("P1", "p2", "highest", "lowish", "priority", "p", "high low"))
    label_lists = st.builds(
        lambda real, others: [*others[::2], *real, *others[1::2]],
        st.lists(names, min_size=1, max_size=3),
        st.lists(st.one_of(near_misses, text), max_size=5),
    )

    @given(label_lists)
    def test_it_always_answers_with_a_priority(self, names: list[str]) -> None:
        """Arbitrary names beside real ones, and an answer rather than a raise for all of them."""
        assert parse_priority(names) in set(Priority)

    @given(label_lists)
    def test_order_does_not_change_the_answer(self, names: list[str]) -> None:
        """Labels arrive in whatever order GitHub feels like."""
        assert parse_priority(names) is parse_priority(list(reversed(names)))

    @given(label_lists, label_lists)
    def test_adding_labels_never_lowers_the_priority(
        self, first: list[str], second: list[str]
    ) -> None:
        """The exact law, which is what the name claims: the higher of the two halves wins.

        This used to assert `>= min(...)`, which is unviolatable. It holds for max semantics, for
        MIN semantics - the one bug worth catching, since it buries urgent work - and for a stub
        that always answers UNSET, because UNSET is rank 0 and `>=` 0 is every answer there is.
        """
        rank = {Priority.UNSET: 0, Priority.LOW: 1, Priority.MEDIUM: 2, Priority.HIGH: 3}
        assert rank[parse_priority(first + second)] == max(
            rank[parse_priority(first)], rank[parse_priority(second)]
        )


class TestLinkParsing:
    # 395/400 draws reach `_parse_number`, which is the one call in the parser that can raise
    # something of its own making. Owner, repository and kind are drawn from valid values on
    # purpose: with the malformed ones mixed in, only 12/400 draws got past the earlier guards,
    # and those guards are covered case by case in `tests/unit/github/test_urls.py`.
    #
    # 173/400 carry a number made of digits `str.isdigit` accepts. That is the reach a table
    # cannot have: `isdigit` is true of 878 codepoints outside ASCII, `int()` raises on 128 of
    # them, and the table names three.
    non_ascii_digits = st.sampled_from([chr(c) for c in range(0x80, 0x110000) if chr(c).isdigit()])
    numbers = st.one_of(
        st.integers(min_value=0, max_value=10**9).map(str),
        st.lists(non_ascii_digits, min_size=1, max_size=3).map("".join),
        # The last is an Arabic-Indic seven beside an ASCII one, which `str.isdigit` accepts
        # whole: a mixed-script number is the case a per-character check would wave through.
        st.sampled_from(("", "-1", "7.0", "1e3", "+7", " 7", "00", "9" * 400, chr(0x667) + "7")),
    )
    links = st.builds(
        lambda owner, repo, kind, number, tail: "/".join(
            ["https://github.com", owner, repo, kind, number, *tail]
        ),
        st.sampled_from(("some-owner", "o", "Canon-Regularis", "a-b-c")),
        st.sampled_from(("some.repo", "n", "Shannon-bot", "repo.git")),
        st.sampled_from(("pull", "issues")),
        numbers,
        # Arbitrary deep-link tail. It sits past the number, so it cannot stop the parse reaching
        # it, which is how the "whatever somebody typed" half is kept without losing the hazard.
        st.lists(st.text(max_size=12), max_size=2),
    )

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

    @given(st.one_of(links, links.map(lambda link: f"<{link}>"), text))
    @settings(max_examples=400)
    def test_it_never_raises_anything_but_its_own_error(self, link: str) -> None:
        """Whatever someone types after `/pr`, they should get an answer rather than a crash."""

        for parse in (parse_pull_request_url, parse_issue_url):
            with contextlib.suppress(UnparseableLinkError):
                parse(link)


class TestTimestamps:
    # 399/400 draws reach `datetime.fromisoformat`, and 273/400 come back as a real instant. The
    # old strategy sent 200/200 to the first guard and None out of it, so the parse never ran.
    # `None` and an int stay with `tests/unit/domain/test_time.py::test_nonsense_is_none`, which
    # names both. A bool is not named anywhere, and does not need to be: `isinstance(True, str)` is
    # False, so it leaves by the same door as the int beside it.
    stamps = st.one_of(
        aware.map(lambda moment: moment.isoformat()),
        # What GitHub actually puts on the wire, which `isoformat` does not produce.
        aware.map(lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")),
        st.sampled_from(
            (
                "2024-13-45T99:99:99Z",
                "2026-08-11T12:00:00",
                "1970-01-01",
                "+010000-01-01T00:00:00Z",
                "0",
                "whenever",
                "2026-08-11T12:00:00+05:00",
                "9" * 500,
            )
        ),
        text,
    )

    @given(stamps)
    @settings(max_examples=300)
    def test_parsing_never_raises(self, value: str) -> None:
        """`fromisoformat` raises ValueError and nothing else is caught, so anything else here is
        a 500 on a webhook."""
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
    @given(aware)
    def test_a_missing_timestamp_is_no_evidence(self, moment: datetime) -> None:
        """Either side missing means there is nothing to compare, so nothing to conclude.

        `None` is written rather than drawn. It used to arrive through `st.none()` as a second
        parameter, which spends a draw on a constant and reads as though the strategy decided it.
        """
        assert is_superseded(moment, None) is False
        assert is_superseded(None, moment) is False

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


def an_issue(title: str, *, number: int = 1) -> IssueSnapshot:
    """The least an issue can be, for the properties that are only about one of its fields.

    Written out rather than splatted from a dict: this file is not on either checker's ratchet, so
    a `**overrides` of `object` would have needed a suppression to pass, and a suppression is the
    thing the ratchet exists to stop being added.
    """
    return IssueSnapshot(
        repository=REPO,
        github_object_id=1,
        number=number,
        title=title,
        html_url="",
        state="open",
    )


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
        # Named rather than left to inference. `st.builds` fills only the arguments that have no
        # default, and this one has, so leaving it out meant every generated block carried an
        # empty description and the invariants below never saw one.
        body=st.text(max_size=3000),
    )
    # The same block, with a title big enough that the card cannot hold it. 120/120 of these need
    # trimming; 0/120 of the strategy above do, whatever is put in its body.
    oversized = st.builds(
        IssueSnapshot,
        repository=st.just(REPO),
        github_object_id=st.integers(min_value=1),
        number=st.integers(min_value=1, max_value=10**7),
        title=huge_titles,
        html_url=text,
        state=st.sampled_from(["open", "closed"]),
        author=st.one_of(st.none(), actors),
        assignees=st.lists(actors, max_size=5).map(tuple),
        labels=st.lists(labels, max_size=5).map(tuple),
        body=st.text(max_size=3000),
    )

    @given(
        st.one_of(metadata, oversized),
        st.sampled_from(list(Status)),
        st.sampled_from(list(Priority)),
    )
    @settings(max_examples=200)
    def test_an_issue_block_always_fits_a_card(
        self, snapshot: IssueSnapshot, status: Status, priority: Priority
    ) -> None:
        card = format_issue(snapshot, status=status, priority=priority).trimmed()

        assert card.length() <= PANEL_BUDGET

    @given(st.one_of(metadata, oversized), st.sampled_from(list(Status)))
    @settings(max_examples=200)
    def test_an_issue_block_never_loses_a_field(
        self, snapshot: IssueSnapshot, status: Status
    ) -> None:
        """Truncation must not silently drop the fields at the bottom of the block.

        Both halves on purpose, and they are complementary rather than co-reachable: the field
        list can only be asserted on a block that was NOT cut, because a title big enough to
        fill the card on its own leaves no room under it and the rows below the name are what
        `fit` takes.

        The question asked is whether the block NEEDED cutting, measured before the cut. Asked of
        the length afterwards - which is what this did - it is always under the budget, because
        that is what `trimmed` guarantees. The gate was therefore all but always true, and the
        first draw that overflowed found that out: a block cut down to its title alone went into
        the half that asserts every field is present. The old strategy could not produce one, so
        a test that was wrong as well as unreached read as passing.
        """
        panel = format_issue(snapshot, status=status)
        rendered = panel.trimmed().text

        if panel.length() <= PANEL_BUDGET:
            for field in ("Issue Name", "Type", "State", "Status", "Priority", "Last Updated"):
                assert f"**{field}:**" in rendered
        else:
            # What has to survive when there is room for nothing else: the name of the thing,
            # first, so a reader scanning the channel still knows what the thread is.
            assert rendered.startswith("**Issue Name:**"), "a cut block does not say what it is"

        # There used to be a third assertion here, that the text never ends on a bare
        # `**Description:**` with nothing under it. It is gone because it cannot fail, and two
        # things in the renderer guarantee that between them: `_the_description` returns the label
        # and the body as ONE block, and `Panel.trimmed` pops whole blocks from the end and only
        # ever `fit`s the one block it cannot pop. A panel here has at most two blocks, so the
        # description is always dropped whole and the fields block is the only one ever cut.
        #
        # If either of those changes - a description split across blocks, or a trim that cuts
        # rather than pops - this is the assertion to put back, and it would then have teeth.

    @given(
        st.builds(
            PullRequestSnapshot,
            repository=st.just(REPO),
            github_object_id=st.integers(min_value=1),
            number=st.integers(min_value=1, max_value=10**7),
            title=st.one_of(text, huge_titles),
            html_url=text,
            state=st.sampled_from(["open", "closed"]),
            merged=st.booleans(),
            reviewers=st.lists(actors, max_size=5).map(tuple),
            body=st.text(max_size=3000),
        ),
        st.sampled_from(list(Status)),
    )
    @settings(max_examples=200)
    def test_a_pull_request_block_always_fits_a_card(
        self, snapshot: PullRequestSnapshot, status: Status
    ) -> None:
        assert format_pull_request(snapshot, status=status).trimmed().length() <= PANEL_BUDGET

    # 200/200 draws are over the preview limit. `st.text(max_size=4000)` was over it in 0/150.
    # Padded with a non-space character at BOTH ends, and that is not decoration. `cut` strips
    # before it measures, so a draw of exactly 701 characters beginning or ending in whitespace
    # strips to 700, is never cut, and carries no ellipsis - which fails the assertion below. The
    # bare `st.text(min_size=701)` passed 120 examples by luck; this one cannot draw that case,
    # because `strip()` leaves at least 702 characters whatever is between the padding.
    long_bodies = st.builds(
        lambda body: f"x{body}x", st.text(min_size=COMMENT_PREVIEW_LIMIT, max_size=2000)
    )

    def a_comment(self, body: str, when: datetime | None = None) -> CommentSnapshot:
        return CommentSnapshot(
            repository=REPO,
            item_number=1,
            comment_id=1,
            object_type=ObjectType.ISSUE,
            html_url="https://github.com/o/n/issues/1#issuecomment-1",
            body=body,
            author=Actor("octocat"),
            created_at=when,
        )

    @given(st.one_of(text, long_bodies), st.one_of(st.none(), aware))
    @settings(max_examples=200)
    def test_a_comment_always_fits_a_card(self, body: str, when: datetime | None) -> None:
        rendered = format_comment(self.a_comment(body, when))

        assert rendered.length() <= PANEL_BUDGET
        assert "**octocat** commented" in rendered.blocks[0].text

    # The names a body might write, against a map that answers for some of them and not others.
    # `@octocat` is the comment's own author as well, which is the case worth drawing: the heading
    # above the body carries a live mention this bot built, so a swap handed the assembled message
    # rather than the body alone could read it back as a name and rewrite it into somebody else.
    _NAMEABLE = st.sampled_from(
        ("@hubot", "@octocat", "@canon/backend", "@everyone", "@here", "<@7>", "@a-b", "@1")
    )
    bodies_naming_people = st.builds(
        lambda pieces: "\n".join(pieces), st.lists(st.one_of(text, _NAMEABLE), max_size=10)
    )

    @given(bodies_naming_people)
    @settings(max_examples=300)
    def test_every_live_mention_in_a_comment_is_one_the_map_answered_for(self, body: str) -> None:
        """Issue #166's half of `TestNothingTypedBecomesAMention`, which guards the other
        direction. A comment body is rendered rather than escaped now, so what stops text somebody
        typed becoming a live mention is no longer a backslash in front of every marker - it is
        the zero-width space `rich_text` puts inside anything already mention-shaped, and the
        lookbehind that refuses an `@` sitting against one.

        Asserted over the BODY block alone, which is also the rule the renderer follows: the line
        above it carries a mention this bot built, live and never defused, and a GitHub login may
        be all digits - so handing the assembled message to the swap would let `<@7>` be read as
        the name `7` and rewritten into whoever is linked under it.
        """
        linked = {"hubot": 909}
        roles = {"backend": 777}

        card = format_comment(self.a_comment(body), linked, roles)
        quoted = next((block.text for block in card.blocks if block.kind is BlockKind.BODY), "")

        assert set(re.findall(r"<@(\d+)>", quoted)) <= {"909"}, (
            "a live person mention came out of text nobody answered for"
        )
        assert set(re.findall(r"<@&(\d+)>", quoted)) <= {"777"}, (
            "a live role mention came out of text nobody answered for"
        )
        assert "@everyone" not in quoted
        assert "@here" not in quoted

        # The markers stay balanced through the swap as well as through the conversion. `rewrite`
        # only ever inserts `<@id>`, which carries neither - but it is the swap that runs last, so
        # this is where the claim has to hold rather than where `rich_text` leaves it.
        assert quoted.count("**") % 2 == 0
        assert quoted.count("```") % 2 == 0

    @given(long_bodies)
    @settings(max_examples=120)
    def test_a_long_comment_body_is_always_cut(self, body: str) -> None:
        """To the COMMENT limit, which is the claim the name makes and five times tighter than the
        card's.

        Asserted against `PANEL_BUDGET`, this said only what the property above it already says:
        the panel bound holds for an uncut body too, so the preview cut could have been deleted
        outright and both would have stayed green.

        Two things changed with issue #166, which rendered a comment body rather than escaping
        it. The cut mark is no longer the LAST thing in the string: `_balanced` closes a marker the
        cut took the other half of, and it appends after the ellipsis. And there are no escapes to
        remove, so the old normalisation measured nothing.

        What is left is a generous bound rather than an exact width, and deliberately so. The cut
        is what enforces the limit; everything after it can only add a bounded amount - a
        zero-width space per construct, the markers `_balanced` closes, and a host named in place
        of a link pointing off GitHub. Twice the limit is comfortably above all of that and still
        far below the panel bound the property above it asserts, so a body cut to the wrong limit,
        or not cut at all, still fails here.
        """
        quoted = next(
            block.text
            for block in format_comment(self.a_comment(body)).blocks
            if block.kind is BlockKind.BODY
        )

        assert "…" in quoted, "a body over the preview limit was published whole"
        assert len(quoted) <= 2 * COMMENT_PREVIEW_LIMIT, "cut, but not to the comment limit"


class TestThreadNames:
    # 400/400 of these need truncating. The old strategy's longest draw was 35 characters against
    # a limit of 100, so the truncation it is named for never ran once.
    long_titles = st.text(min_size=THREAD_NAME_LIMIT, max_size=250)

    @given(long_titles, st.integers(min_value=1, max_value=10**7))
    def test_a_thread_name_always_fits_discord(self, title: str, number: int) -> None:
        assert (
            len(truncate_thread_name(thread_name(an_issue(title, number=number))))
            <= THREAD_NAME_LIMIT
        )

    @given(text)
    def test_a_thread_name_is_never_empty(self, title: str) -> None:
        """Discord rejects a blank thread name.

        Short text on purpose: the hazard here is the opposite one, and `st.text(max_size=200)`
        draws the empty string and the all-whitespace string readily.
        """
        assert truncate_thread_name(title).strip() != ""


# Every key any mapper reads. Excluded from the arbitrary-JSON keys below so that "the mapper
# answered with nothing" can be asserted STRICTLY rather than as "nothing or something": a
# generated key is only almost never one of these, and almost never is what makes a test flaky.
_READ_BY_A_MAPPER = frozenset(
    {"id", "number", "name", "login", "html_url", "owner", "pull_request", "title", "body"}
)

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
        st.dictionaries(
            st.text(max_size=12).filter(lambda key: key not in _READ_BY_A_MAPPER),
            children,
            max_size=6,
        ),
    ),
    max_leaves=15,
)

# Every field a mapper reads, drawn from `json_values`, so each is PRESENT and arbitrarily typed.
# That is the claim: a field GitHub sent as something unexpected must be refused, not raised on.
_ARBITRARY = (
    "user",
    "title",
    "body",
    "state",
    "labels",
    "assignees",
    "requested_reviewers",
    "requested_teams",
    "updated_at",
    "closed_at",
    "merged",
    "merged_at",
    "head",
    "draft",
    "pull_request",
    "avatar_url",
    "diff_hunk",
    "path",
    "line",
    "full_name",
    "slug",
    "color",
    "node_id",
    "private",
)
# The handful every mapper GUARDS on, given the right type so the body below the guard is
# reached. 300/300 draws build an issue, an actor and a repository; the old strategy built
# 0/300 of any of them, because `st.text(max_size=12)` does not produce the key `login`.
shaped_body = st.fixed_dictionaries(
    {
        "id": st.integers(),
        "number": st.integers(),
        "name": st.text(min_size=1, max_size=12),
        "login": st.text(min_size=1, max_size=12),
        "html_url": st.text(min_size=1, max_size=20),
        "owner": st.fixed_dictionaries({"login": st.text(min_size=1, max_size=12)}),
    },
    optional=dict.fromkeys(_ARBITRARY, json_values),
)


class TestTheFieldMappersAgainstArbitraryValues:
    """Webhook bodies come off the network. A mapper that raises takes the request down with it.

    Handed the value directly rather than wrapped in a payload. Five parser properties used to
    sit here doing the latter, and every draw died at `if action not in SUPPORTED`, because
    `st.text` does not produce "opened": 1,500 examples to run one early return, asserting
    nothing. The parsers are covered by tables in `tests/unit/github/`, which can name the field
    that broke. This one reaches the bodies and says what has to be true of them.

    Two properties rather than one, because the two reach different depths and mixing them cost
    both: a 3-to-1 mix of the shaped body with arbitrary JSON built an issue in 67 draws out of
    300, where the shaped body alone builds one in 300 out of 300.
    """

    @given(json_values)
    @settings(max_examples=150)
    def test_nothing_at_all_gets_past_the_guards(self, value: object) -> None:
        """A list, a float, a bare string, a dict of keys nothing reads. Every mapper turns each of
        those away, and the answer is asserted exactly.

        `is None or isinstance(..., Actor)` is what this used to say, which is the declared return
        type restated: it holds for every implementation there could be, including one that reads
        nothing and one that raises nowhere near here. The strategy excludes every key a mapper
        reads - `_READ_BY_A_MAPPER` - so the nothing answer is guaranteed and can be demanded.
        """
        assert mapping.actor(value) is None
        assert mapping.actors(value) == ()
        assert mapping.labels(value) == ()
        assert mapping.is_pull_request(value) is False
        assert mapping.repository(value) is None
        assert mapping.issue(value, REPO) is None
        assert mapping.pull_request(value, REPO) is None
        assert mapping.review_comment(value, REPO, item_number=1) is None

    # 100 rather than the 300 it was measured at: 24 optional fields of recursive JSON apiece is
    # the most expensive draw in the file, 300 of them took 42 seconds and 150 took 26, both
    # against the 60-second `pytest-timeout` in pyproject.toml. A margin of 2.3x on the machine
    # that wrote this is not a margin on a shared CI runner, and the hazard is reached by EVERY
    # draw - 300/300 measured - so the count buys variety, never reach. 100 keeps the variety that
    # matters and the timeout out of the picture.
    @given(shaped_body)
    @settings(max_examples=100)
    def test_a_body_shaped_right_with_arbitrary_fields_never_raises(self, value: object) -> None:
        """Past the guards, where the work is. Every field below the required handful is arbitrary
        JSON, so `labels` meets a string, `assignees` meets a dict, `head` meets a float and
        `updated_at` meets a list - the shapes a real payload takes when GitHub changes something
        or a proxy mangles it."""
        assert isinstance(mapping.actor(value), Actor)
        assert isinstance(mapping.actors(value), tuple)
        assert isinstance(mapping.labels(value), tuple)
        assert isinstance(mapping.is_pull_request(value), bool)
        assert mapping.repository(value) is not None
        assert mapping.issue(value, REPO) is not None
        assert mapping.pull_request(value, REPO) is not None
        mapping.review_comment(value, REPO, item_number=1)


# Mention-shaped text, kept apart from the markup hazards so one of each can be guaranteed. Of
# the markup entries only a few carry `@`, which is why mixing them left the hazard at 52%.
_AT_HAZARDS = st.sampled_from(("@everyone", "@here", "@octocat", "@hubot", "@1", "@a-b"))
_MARKUP_HAZARDS = st.sampled_from(
    (
        "#12",
        "GH-7",
        "gh-42",
        "](",
        "![",
        "```",
        "~~~",
        "<!--",
        "</details>",
        "<@&777000>",
        "@",
        "\\",
        # The three that must appear immediately AFTER a mention to do anything, which is why they
        # are written as tails rather than as standalone noise. A `/` turns the mention this bot
        # handed over into a team; the accented and CJK letters are what the writer used to read as
        # word characters and the reader did not. Without them in the pool the property cannot
        # reach either class, and both were real bugs.
        "/security",
        "/",
        "é@0",
        "中@0",
    )
)
_NOISE = st.one_of(text, _AT_HAZARDS, _MARKUP_HAZARDS)


@st.composite
def _tagged(draw: st.DrawFn) -> tuple[str, dict[int, str]]:
    """A message and the map it was handed, with one id from the map spliced into the text.

    Spliced rather than hoped for. Left to `st.text`, 0/300 draws held a `<@id>` token at all, so
    the swap under test never ran and the assertion below held over text nothing had touched.
    Ids NOT in the map go in too: those are the ones that must come out neutralised.
    """
    people = draw(
        st.dictionaries(
            st.integers(min_value=10**14, max_value=10**19),
            st.from_regex(r"\A[a-z][a-z0-9-]{0,20}\Z"),
            min_size=1,
            max_size=5,
        )
    )
    strangers = draw(st.lists(st.integers(min_value=10**14, max_value=10**19), max_size=2))
    tokens = st.sampled_from([f"<@{who}>" for who in [*people, *strangers]])
    named = f"<@{draw(st.sampled_from(sorted(people)))}>"
    rest = draw(st.lists(st.one_of(tokens, _NOISE), max_size=8))
    return "".join([*rest[::2], named, *rest[1::2]]), people


class TestNothingTypedBecomesAMention:
    """Issue #121. A published transcript may mention the accounts it was handed and no others.

    The strongest test in the set, and the one the whole design rests on. `one_message` neutralises
    what somebody typed in fragments and drops the caller's spellings between them, so no `defuse`
    ever sees a mention this bot built and no fragment can be turned into one. Asserted over
    generated text rather than over the handful of cases anybody thought of, because the thing being
    ruled out is precisely the case nobody thought of.
    """

    @given(_tagged())
    @settings(max_examples=300)
    def test_every_live_at_is_one_it_was_given(self, pair: tuple[str, dict[int, str]]) -> None:
        said, people = pair
        spelled = {who: f"@{login}" for who, login in people.items()}

        published = one_message(said, spelled)

        # Read back with the project's OWN reader rather than a regex written here. The regex this
        # used to carry, `(?<![\w/])@([A-Za-z0-9][A-Za-z0-9-]*)`, could not see a TEAM: handed
        # `@acme` with a typed `/security` after it, it matched `@acme`, found it in the map and
        # passed - while the published line addressed the team `security` and nobody at all. A
        # real bug lived behind that blind spot. An oracle that re-implements the grammar can only
        # ever be as right as whoever wrote it twice.
        read = names_in(published)

        assert set(read.people) <= set(people.values()), (
            "text that was typed came out as a live mention"
        )
        assert read.teams == (), (
            "a team was addressed, and a team is never handed over - the map carries account "
            "logins, so any live team came out of text somebody typed"
        )

    # 300/300 draws hold an `@` followed by an alphanumeric, which is the only thing `defuse` has
    # anything to do. The old strategy held one in 0/300.
    #
    # Joined on a newline rather than concatenated. `_MENTION` will not match an `@` preceded by a
    # word character, so splicing `@octocat` straight after arbitrary text neutralises the hazard
    # by accident: measured at 84%, and the 16% were draws where the piece before it ended in a
    # letter. A newline is not a word character, and is what a Discord message is full of anyway.
    said_by_anybody = st.builds(
        lambda one, rest: "\n".join([*rest[::2], one, *rest[1::2]]),
        _AT_HAZARDS,
        st.lists(_NOISE, max_size=8),
    )

    @given(said_by_anybody)
    @settings(max_examples=300)
    def test_with_nothing_given_nothing_is_live(self, said: str) -> None:
        """The same claim with the map empty, which is every message nobody tagged anybody in."""
        published = one_message(said)

        assert not re.search(r"(?<![\w/])@[A-Za-z0-9]", published)


class TestNothingInAReminderRingsAnybodyElse:
    """Issue #229. A reminder is posted for the whole channel and carries somebody's own words, so
    the only live mentions it may hold are the two this bot wrote: the person it is for, and
    whoever asked for it.

    Every draw holds a hazard, spliced in by construction and joined on newlines for the reason
    `said_by_anybody` gives - including the two ids that ARE allowed live, typed into the message,
    which the body block must defuse like any other.
    """

    _HAZARDS = st.sampled_from(
        ("<@7>", "<@!7>", "<@&7>", "<#7>", "@everyone", "@here", "<@20>", "<@10>")
    )
    messages = st.builds(
        lambda one, rest: "\n".join([*rest[::2], one, *rest[1::2]])[:500],
        _HAZARDS,
        st.lists(text, max_size=4),
    )

    @given(messages, st.booleans())
    @settings(max_examples=200)
    def test_the_only_live_mentions_are_the_ones_this_bot_wrote(
        self, message: str, somebody_else_asked: bool
    ) -> None:
        set_by = 10 if somebody_else_asked else 20
        when = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

        card = format_reminder(
            member_id=20, set_by=set_by, set_at=when, due_at=when, message=message, late=True
        )
        body = next((block.text for block in card.blocks if block.kind is BlockKind.BODY), "")

        assert set(re.findall(r"<@!?(\d+)>", card.text)) <= {"20", str(set_by)}
        assert not re.search(r"<@!?\d+>", body), "a mention typed into the message is live"
        assert not re.search(r"<@&\d+>", card.text), "a role mention is live"
        assert not re.search(r"<#\d+>", card.text), "a channel mention is live"
        assert "@everyone" not in card.text
        assert "@here" not in card.text
