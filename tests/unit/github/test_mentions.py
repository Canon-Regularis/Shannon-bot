"""What counts as a name in a comment body, and what is put in its place.

Two halves that have to agree. One reads the names so the right rows are fetched, the other swaps
them for mentions. A disagreement between them does not fail: it renders a name exactly as an
unlinked name renders, so nothing anywhere says a mention was owed and never happened. That is
what most of this file is about.

The rest is the pattern itself, which is security code. A mention this bot emits is delivered by a
gate set to honour user and role mentions, so the pattern is the only thing standing between a
comment body and a real ping.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.rich_text import as_note_text
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT, clipped
from shannon.github import mentions
from shannon.github.mentions import (
    MENTION_LIMIT,
    Mentioned,
    names_a_note_writes,
    names_in,
    rewrite,
    rewrite_a_note,
)

pytestmark = pytest.mark.unit

PEOPLE = {"john": 111, "octo-cat": 222, "canon": 333}
TEAMS = {"backend": 900, "back_end": 901}


def previewed(body: str) -> str:
    """A body as the ESCAPING leaves it, which is what a note used to be handed.

    Not what the renderer sends any more - see `shown` below - but still a shape this module has
    to read, which is why it is still here rather than deleted. `_SLUG_IN_TEXT` accommodates both
    `_` and `\\_` on purpose, and the defusing these tests lean on is what puts a zero-width space
    inside anything already mention-shaped. Those branches have to be exercised by something.
    """
    return clipped(body, limit=COMMENT_PREVIEW_LIMIT)


def shown(body: str) -> str:
    """A body as the thread actually shows it, which is what BOTH halves are handed.

    The production function rather than a second spelling of what it does. Issue #166 is exactly
    why: this file used to derive the string itself, and when the renderer moved to the rich
    conversion the invariant below went on passing about a string nothing produced any more.
    """
    return as_note_text(body)


def swapped(text: str) -> str:
    return rewrite(
        text,
        person=lambda name: f"<@{PEOPLE[name.lower()]}>" if name.lower() in PEOPLE else None,
        team=lambda name: f"<@&{TEAMS[name.lower()]}>" if name.lower() in TEAMS else None,
    )


def asked_about(text: str) -> set[tuple[bool, str]]:
    """Every name the note swap would look up, which is what the reader has to have covered.

    Through `rewrite_a_note`, the function the renderer calls, rather than `rewrite` with a flag
    this file chose. Handing the two halves different answers about code spans is the one way the
    invariant below could be made to pass while being false, and going through the paired
    functions is what takes that away rather than watching for it.
    """
    seen: set[tuple[bool, str]] = set()

    def note(is_team: bool):
        def render(name: str) -> None:
            seen.add((is_team, name.lower()))
            return None

        return render

    rewrite_a_note(text, person=note(False), team=note(True))
    return seen


class TestWhatIsAName:
    def test_a_login_is_swapped(self) -> None:
        assert swapped("hi @john") == "hi <@111>"

    def test_a_hyphen_inside_a_login_is_part_of_it(self) -> None:
        assert swapped("hi @octo-cat") == "hi <@222>"

    def test_the_case_it_was_written_in_does_not_matter(self) -> None:
        assert swapped("hi @JOHN and @John") == "hi <@111> and <@111>"

    def test_a_team_is_swapped_and_the_organisation_is_dropped(self) -> None:
        """`/link_team` stores a bare slug and the table is scoped to one server, so the slug is
        the whole of what identifies a team."""
        assert swapped("cc @canon-regularis/backend") == "cc <@&900>"

    def test_an_escaped_underscore_is_read_back_as_one(self) -> None:
        """The landmine this file exists for. The slug pattern needs a doubled backslash to mean
        a literal one, and the single-backslash version also compiles and also matches: it stops
        the slug at the first underscore, so this would quietly ping a team called `back`.
        """
        escaped = previewed("cc @canon/back_end")

        assert "back\\_end" in escaped, "the escaping stopped doing the thing this is about"
        assert swapped(escaped) == "cc <@&901>"

    def test_a_plain_underscore_is_read_the_same_way(self) -> None:
        """The reader is handed the raw body and the swap is handed the escaped one, so the same
        slug has to be found in both shapes or the two disagree about what was named."""
        assert names_in("cc @canon/back_end") == names_in(previewed("cc @canon/back_end"))

    def test_a_name_nobody_answers_for_is_left_as_written(self) -> None:
        assert swapped("hi @nobody and @canon/nothing") == "hi @nobody and @canon/nothing"

    def test_text_with_no_name_in_it_comes_back_unchanged(self) -> None:
        assert swapped("nothing to see here") == "nothing to see here"


class TestWhereANameStops:
    def test_a_full_stop_after_a_team_is_not_swallowed(self) -> None:
        assert swapped("cc @canon/backend.") == "cc <@&900>."

    def test_a_full_stop_after_a_login_is_not_swallowed(self) -> None:
        assert swapped("ask @john.") == "ask <@111>."

    def test_an_organisation_with_nothing_after_it_is_not_a_name(self) -> None:
        assert swapped("@canon/ nothing") == "@canon/ nothing"

    def test_an_address_is_not_a_mention(self) -> None:
        """The character before the `@` decides it, which is the same rule GitHub uses."""
        assert swapped("write to john@canon.com") == "write to john@canon.com"

    def test_a_name_in_a_pasted_link_is_not_a_mention(self) -> None:
        assert swapped("see https://example.com/@john") == "see https://example.com/@john"

    @pytest.mark.parametrize(
        "written",
        ["@john-", "@jo--hn", "@-john", "@" + "a" * 40],
        ids=["trailing hyphen", "doubled hyphen", "leading hyphen", "too long"],
    )
    def test_a_shape_github_cannot_issue_is_refused_outright(self, written: str) -> None:
        """Refused whole rather than trimmed to the part that is legal, which is the safer of the
        two and deliberately so. Trimming would read `@jo--hn` as `jo`, and if somebody called
        `jo` had linked an account they would be pinged for a name that is not theirs. Giving up
        costs a mention nobody gets; trimming costs the wrong person one.
        """
        assert names_in(written) == Mentioned(people=(), teams=())

    def test_a_login_of_the_longest_length_github_issues_is_matched_whole(self) -> None:
        longest = "a" * 39

        assert names_in(f"@{longest}").people == (longest,)


class TestANameThePreviewCutInHalf:
    """A comment is shown as a preview, and the cut lands mid-word. What is left against the cut
    marker is a prefix of somebody's name rather than a name, and rendering it would ping a person
    the comment never named: `@monalisa` shows as `@mona`, and `mona` may well be linked."""

    def test_a_login_cut_short_is_not_a_name(self) -> None:
        # A space before the `@`, or the character in front of it would refuse the name on its
        # own and this would pass without the cut marker doing anything.
        cut = previewed("x" * (COMMENT_PREVIEW_LIMIT - 6) + " @monalisa")

        assert cut.endswith(" @mona…"), "the preview stopped cutting where this expects"
        assert names_in(cut) == Mentioned(people=(), teams=())

    def test_a_team_cut_short_is_not_a_name_either(self) -> None:
        """And not a shorter one. The slug would otherwise give back a character at a time until
        it found something that fit, so `@canon/back…` came out as a team called `bac`."""
        assert names_in("@canon/back…") == Mentioned(people=(), teams=())

    def test_a_name_the_cut_did_not_reach_is_still_read(self) -> None:
        assert names_in("@monalisa and @canon/backend…").people == ("monalisa",)


class TestNothingAlreadyDefusedIsPutBack:
    """The escaping puts a zero-width space inside anything that already looked like a mention.
    A name carrying one has been taken apart on purpose, and the pattern refuses it.

    Each of these puts the name in the map as well, so what is being proved is that the pattern
    turns it away rather than the lookup happening to miss."""

    def test_a_mass_mention_cannot_come_back(self) -> None:
        text = previewed("@everyone @here look")

        assert rewrite(text, person=lambda name: "<@66>", team=lambda name: "<@&66>") == text, (
            "a defused mass mention was read as a login"
        )

    def test_a_user_mention_written_by_hand_cannot_come_back(self) -> None:
        text = previewed("ping <@1234567> please")

        assert rewrite(text, person=lambda name: "<@66>", team=lambda name: "<@&66>") == text

    def test_a_role_mention_written_by_hand_cannot_come_back(self) -> None:
        text = previewed("ping <@&1234567> please")

        assert rewrite(text, person=lambda name: "<@66>", team=lambda name: "<@&66>") == text

    def test_a_mention_this_bot_built_itself_is_not_rewritten(self) -> None:
        """A login may be all digits, and the header of a note carries a live `<@7>` that has
        never been defused. The rewrite is only ever handed a quoted body, and the `<` in the
        lookbehind is what makes that a guard rather than only a rule.
        """
        assert rewrite("**<@7>** commented", person=lambda name: "<@66>", team=lambda n: None) == (
            "**<@7>** commented"
        )


class TestHowManyAreAnswered:
    def test_the_reader_stops_at_the_limit(self) -> None:
        written = " ".join(f"@u{i}" for i in range(MENTION_LIMIT + 5))

        assert len(names_in(written).people) == MENTION_LIMIT

    def test_the_swap_stops_at_the_limit(self) -> None:
        written = " ".join(f"@u{i}" for i in range(MENTION_LIMIT + 5))
        rendered = rewrite(written, person=lambda name: "<@66>", team=lambda name: None)

        assert rendered.count("<@66>") == MENTION_LIMIT
        assert f"@u{MENTION_LIMIT}" in rendered, "the name past the limit was not left as written"

    def test_the_budget_counts_names_and_not_how_often_they_are_written(self) -> None:
        """Both halves count distinct names, which is what keeps them reaching the same distance
        into a body. Counting occurrences in the swap would let it look at names the reader never
        asked about."""
        written = " ".join(["@john"] * 30) + " @octo-cat"

        assert swapped(written).endswith("<@222>"), "a repeated name ate the budget"

    def test_a_name_written_twice_reads_the_same_both_times(self) -> None:
        assert swapped("@john and @john again") == "<@111> and <@111> again"

    def test_people_and_teams_have_their_own_budgets_counted_together(self) -> None:
        """One limit over both, because the thing being bounded is how many people one comment
        can notify, and a role reaches more of them than a person does."""
        written = " ".join(f"@o/t{i}" for i in range(MENTION_LIMIT + 3))

        assert len(names_in(written).teams) == MENTION_LIMIT


class TestTheReaderCoversTheSwap:
    """The invariant, in both directions. The two halves must read exactly the same names.

    A name the swap looks up that the reader never asked about is a mention that silently does not
    happen: a name with no row renders exactly as a name nobody has linked, and nothing records the
    difference. That is the direction this class was written for.

    The other direction is not merely wasteful, which is why it is asserted too. The reader and the
    swap share one budget of ten distinct names, so a reader that reads WIDER than the swap can
    spend the budget on names the swap will never ask about - and the real name behind them is then
    never fetched, which is the same silent loss arriving by the opposite road. A subset assertion
    cannot see that, and a mutation that changed the reader's mind alone passed seven hundred tests
    before this said equality.
    """

    @pytest.mark.parametrize(
        "body",
        [
            "hi @john and @octo-cat",
            "cc @canon/back_end and @canon/backend.",
            "@everyone @here <@123> <@&456> a@b.com",
            "@JOHN @john @John",
            "x" * (COMMENT_PREVIEW_LIMIT - 5) + "@monalisa",
            "**@john** `@octo-cat` [@canon](https://x)",
            " ".join(f"@u{i}" for i in range(MENTION_LIMIT + 5)),
            # Issue #166. A code span and a fenced block, which the swap now steps over - so the
            # reader has to step over the same ones or it spends the shared budget on names the
            # swap will never reach.
            "`@john` and @octo-cat",
            "```\n@john\n```\n@canon/backend after it",
            "@john `@octo-cat` @canon/back_end `@nobody`",
            # The budget, starved on purpose: ten distinct names inside code spans and a real
            # one behind them. A reader that read wider than the swap would spend the whole
            # allowance on names nothing renders and never fetch a row for `@john`.
            " ".join(f"`@u{i}`" for i in range(MENTION_LIMIT)) + " @john",
        ],
    )
    def test_everything_the_swap_asks_about_was_read_first(self, body: str) -> None:
        # One string, prepared once by the production function, and asked of the two paired
        # functions production calls. Nothing here chooses a policy, which is the point: this test
        # cannot be made to pass by asking the halves different questions.
        said = shown(body)
        read = names_a_note_writes(said)
        covered = {(False, name) for name in read.people} | {(True, name) for name in read.teams}

        asked = asked_about(said)
        assert asked <= covered, (
            "the swap looked up a name the reader never asked about, which is a mention that "
            "silently does not happen"
        )
        assert covered <= asked, (
            "the reader read a name the swap will never ask about - they share a budget of ten "
            "distinct names, so reading wider spends it on names nothing will render"
        )


class TestTheShapesThisAlsoOwns:
    """`/link` and `/link_team` validate what somebody types against the same rules."""

    @pytest.mark.parametrize("name", ["monalisa", "mona-lisa", "a", "a" * 39, "1"])
    def test_a_login_github_could_issue_is_accepted(self, name: str) -> None:
        assert mentions.is_login(name)

    @pytest.mark.parametrize("name", ["-mona", "mona-", "mona--lisa", "a" * 40, "mona_lisa", ""])
    def test_a_login_github_could_not_issue_is_refused(self, name: str) -> None:
        assert not mentions.is_login(name)

    @pytest.mark.parametrize("name", ["backend", "back_end", "back.end", "back-end", "a" * 99])
    def test_a_team_slug_is_more_forgiving_than_a_login(self, name: str) -> None:
        assert mentions.is_team_slug(name)

    @pytest.mark.parametrize("name", ["_backend", "", "a" * 100, "back end"])
    def test_a_slug_github_could_not_build_is_refused(self, name: str) -> None:
        assert not mentions.is_team_slug(name)


def test_the_two_kinds_are_kept_apart() -> None:
    """A slug that happens to match a login is not that person. They live in different tables and
    Discord writes them with different syntax."""
    read = names_in("@canon and @canon/backend")

    assert read == Mentioned(people=("canon",), teams=("backend",))
