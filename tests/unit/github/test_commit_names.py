"""Which hashes a body names, read by their shape the way GitHub links them. Issue #186.

A table more than a story, because the rule is a table: what stands on each side of a run of hex
decides whether it is a commit or part of something else, and every row below is a neighbour that
was decided one way or the other on purpose.
"""

from __future__ import annotations

import pytest

from shannon.domain.models import RepositoryRef
from shannon.github.commit_names import NamedCommit, commits_named_in

pytestmark = pytest.mark.unit

FULL = "a1b2c3d4" * 5


def shas(text: str) -> list[str]:
    return [named.sha for named in commits_named_in(text)]


class TestWhatIsAHash:
    @pytest.mark.parametrize(
        ("text", "found"),
        [
            ("abc1234", ["abc1234"]),
            ("Fixed in abc1234.", ["abc1234"]),
            ("(abc1234)", ["abc1234"]),
            ("abc1234, def5678 and 0123abc", ["abc1234", "def5678", "0123abc"]),
            (f"This reverts commit {FULL}.", [FULL]),
            (f"(cherry picked from commit {FULL})", [FULL]),
            ("abc1234: fix the parser", ["abc1234"]),
            ("landed in abc1234!", ["abc1234"]),
            ("abc1234's parent", ["abc1234"]),
            ('"abc1234"', ["abc1234"]),
            ("**abc1234**", ["abc1234"]),
            ("*abc1234*", ["abc1234"]),
            ("~~abc1234~~", ["abc1234"]),
            ("||abc1234||", ["abc1234"]),
            ("- abc1234", ["abc1234"]),
            ("> abc1234", ["abc1234"]),
            ("first\nabc1234\nlast", ["abc1234"]),
            ("abcdef1", ["abcdef1"]),
            ("a1b2c3d", ["a1b2c3d"]),
        ],
        ids=[
            "alone",
            "ending a sentence",
            "in parentheses",
            "three in a list",
            "a revert's whole hash",
            "a cherry-pick's whole hash",
            "before a colon and a space",
            "before an exclamation mark",
            "before an apostrophe",
            "in quotes",
            "in bold",
            "in italics",
            "struck through",
            "behind a spoiler",
            "a list item",
            "a quotation",
            "a line of its own",
            "seven characters",
            "digits and letters turn about",
        ],
    )
    def test_a_hash_standing_alone_is_found(self, text: str, found: list[str]) -> None:
        assert shas(text) == found


class TestWhatIsNotOne:
    @pytest.mark.parametrize(
        "text",
        [
            "abc123",
            "a" * 40 + "1",
            "aaaaaaa",
            "deadbeef",
            "defaced",
            "1234567",
            "20261015",
            "Released on 20261015 for everyone",
            "the deadbeef marker moved in v2",
            "<@1234567>",
            "<​@1234567>",
            "<@12345678901234567>",
            "@​12345678901234567",
            "<t:1700000000:f>",
            "sha256:" + "ab12" * 16,
            "ABC1234",
            "aBc1234",
            "a1Bc234",
        ],
        ids=[
            "six characters",
            "forty-one characters",
            "no digit",
            "a hex word",
            "an English word",
            "no letter",
            "a date",
            "a date with letters after it",
            "a hex word with a digit after it",
            "a hand-typed mention",
            "a defused mention",
            "a Discord id",
            "an escaped Discord id",
            "a timestamp",
            "a sha256 digest",
            "uppercase",
            "mixed case",
            "an uppercase letter past the first digit",
        ],
    )
    def test_it_is_left_as_written(self, text: str) -> None:
        assert shas(text) == []


class TestStandingAlone:
    @pytest.mark.parametrize(
        "text",
        [
            "xabc1234",
            "abc1234x",
            "éabc1234",
            "提交abc1234",
            "_abc1234_",
            "0xabc1234",
            "v2.1.0-14-g2414abc",
            "Change-Id: I1234abcdef",
            "octocat@abc1234",
            "@abc1234",
            "abc1234@alice",
            "user@abc1234.com",
            "x.abc1234",
            "@org/team.abc1234",
            "@alice-abc1234",
            "-abc1234",
            "abc1234-dirty",
            "123e4567-e89b-12d3-a456-426614174000",
            "/abc1234",
            "abc1234/file",
            "a/b/c@abc1234",
            "#a1b2c3d4",
            "abc1234#L10",
            "\\abc1234",
            "abc1234\\_x",
            "abc1234[0]",
            "abc1234`",
            "[abc1234]",
            "abc1234]",
            "[abc1234 is the one](https://evil.example/a b)",
            "[x]abc1234",
            "!abc1234",
            "abc1234..def5678",
            "abc1234...def5678",
            "abc1234.txt",
            "abc1234:README.md",
            "<:abc1234:123456789012345678>",
            "abc1234…",
            "abc1234.…",
            "abc1234:…",
        ],
        ids=[
            "a letter before",
            "a letter after",
            "an accented letter before",
            "a word in another script before",
            "underscores",
            "a hex literal",
            "git describe",
            "a Gerrit change id",
            "a fork's spelling",
            "a mention",
            "a name after",
            "an email address",
            "a dot before",
            "the end of a team slug",
            "the end of a login",
            "a hyphen before",
            "a hyphen after",
            "a UUID",
            "a slash before",
            "a slash after",
            "a path in front",
            "a colour",
            "a line anchor after",
            "an escape",
            "an escape after",
            "an index after",
            "a backtick after",
            "brackets",
            "a closing bracket after",
            "opening the label of a link the cut or a space broke",
            "a closing bracket before",
            "an exclamation mark before",
            "a two-dot range",
            "a three-dot range",
            "a file name",
            "a path after a colon",
            "a custom emoji",
            "the cut mark",
            "the cut mark after a dot",
            "the cut mark after a colon",
        ],
    )
    def test_a_hash_that_is_part_of_something_else_is_not_read(self, text: str) -> None:
        assert shas(text) == []

    def test_a_name_beside_a_hash_keeps_the_hash_out_of_it(self) -> None:
        """Read either way round, a link would cut a name short or leave a live one behind it."""
        assert shas("@alice-abc1234 @org/team.abc1234 abc1234@alice") == []


class TestWhereGitHubReadsNothing:
    """Code as Discord draws it, because a link written into code is shown as its markup."""

    @pytest.mark.parametrize(
        "text",
        [
            "`abc1234`",
            "``abc1234``",
            "`` x abc1234 y ``",
            "`x`abc1234",
            "run `git show abc1234 --stat`",
            "Undo it with `git revert --no-edit\nabc1234 && git push` before Friday",
            "Pinned with ``git checkout\nabc1234 -- src`` for now",
            "Run ``make``` and abc1234 is the fix",
            "`x``` then abc1234",
            "```\nabc1234\n```",
            "```\nabc1234",
            "x ```abc1234``` y",
            "``````md\nFixed in abc1234\n```\nlog\n```\n``````",
            "`````````\nabc1234\n``)",
            "[abc1234](https://evil.example)",
            "[see abc1234 here](https://github.com/o/r)",
            "[x](abc1234)",
            "https://github.com/o/n/commit/abc1234",
            "<https://github.com/o/n/commit/abc1234>",
            "https://e.x/?sha=abc1234",
            "http://e.x/abc1234",
            "http://ci.example/job?rev=abc1234",
        ],
        ids=[
            "a code span",
            "a double-backtick span",
            "inside a double-backtick span",
            "against a code span",
            "inside a code span, away from its backticks",
            "a code span across a line",
            "a double-backtick span across a line",
            "a closer that is the front of a fence",
            "a single closer that is the front of a fence",
            "a fence",
            "a fence the cut left open",
            "a fence mid-line",
            "a six-backtick fence holding three-backtick ones",
            "a span opening on the backticks a fence closed on",
            "a link's label",
            "inside a link's label",
            "a link's address",
            "a commit's address",
            "an address in angle brackets",
            "a query string",
            "an http address",
            "an http address's query string",
        ],
    )
    def test_a_hash_there_is_not_read(self, text: str) -> None:
        assert shas(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "`x` abc1234",
            "``x`` abc1234",
            "```\ny\n``` abc1234",
            "[x](https://e.x) abc1234",
            "https://e.x abc1234",
        ],
        ids=["a code span", "a double span", "a fence", "a link", "an address"],
    )
    def test_a_hash_after_one_is_still_read(self, text: str) -> None:
        assert shas(text) == ["abc1234"]


class TestAnotherRepository:
    @pytest.mark.parametrize(
        ("text", "owner", "name"),
        [
            ("o/r@abc1234", "o", "r"),
            ("see some-owner/my_repo.v2@abc1234.", "some-owner", "my_repo.v2"),
            ("o/r_@abc1234", "o", "r_"),
            ("o/r-@abc1234", "o", "r-"),
        ],
        ids=["short", "dots and underscores", "ending on an underscore", "ending on a hyphen"],
    )
    def test_the_repository_written_in_front_comes_with_the_hash(
        self, text: str, owner: str, name: str
    ) -> None:
        assert list(commits_named_in(text)) == [
            NamedCommit(
                start=text.index(owner),
                end=text.index("abc1234") + 7,
                sha="abc1234",
                repository=RepositoryRef(owner=owner, name=name),
            )
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "o/r.@abc1234",
            "o/..@abc1234",
            "-o/r@abc1234",
            "a" * 40 + "/r@abc1234",
            "o/" + "r" * 101 + "@abc1234",
            "o/r@ABC1234",
        ],
        ids=[
            "a name ending on a dot",
            "two dots",
            "an owner opening on a hyphen",
            "an owner past thirty-nine characters",
            "a name past a hundred characters",
            "an uppercase hash",
        ],
    )
    def test_one_github_could_not_have_written_is_not_read_at_all(self, text: str) -> None:
        """Not even as a bare hash: it would link into the wrong repository."""
        assert shas(text) == []

    def test_a_bare_hash_belongs_to_nobody_written(self) -> None:
        assert [named.repository for named in commits_named_in("abc1234")] == [None]


class TestWhereItWasWritten:
    def test_each_is_found_where_it_was_written_in_order(self) -> None:
        text = "see abc1234 and o/r@def5678, then `ffff999` and 9999fff"

        found = list(commits_named_in(text))

        assert [text[named.start : named.end] for named in found] == [
            "abc1234",
            "o/r@def5678",
            "9999fff",
        ]

    def test_one_at_either_end_is_found(self) -> None:
        assert shas("abc1234 x def5678") == ["abc1234", "def5678"]


class TestWhatTheShapeGetsWrong:
    """Shape alone cannot tell these from a commit, and GitHub would leave them as text.

    Pinned so the trade the issue made stays visible: each of these links to GitHub's 404.
    """

    @pytest.mark.parametrize("text", ["ed25519", "face2face", "602214076e15"])
    def test_a_hex_word_with_a_digit_is_read_as_a_commit(self, text: str) -> None:
        assert shas(text) == [text]
