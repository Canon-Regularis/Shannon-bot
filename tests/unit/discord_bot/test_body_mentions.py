"""Names written in a comment body, once they have been through the real escaping.

The module next door proves the pattern against text handed to it directly. This proves the two
layers actually meet: every case here goes through `format_comment` or `format_review`, so the
body has been cut, escaped and quoted by the same code that runs in production before the swap
ever sees it.

Several of these put the dangerous name in the map on purpose. That is the point. Asserting a
mass mention does not resolve while nothing could have resolved it proves nothing about the
guard; it has to be refused while something is standing ready to answer for it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import format_comment, format_review
from shannon.discord_bot.safe_text import MESSAGE_LIMIT
from shannon.domain.enums import ObjectType
from shannon.domain.models import Actor, CommentSnapshot, RepositorySnapshot, ReviewSnapshot
from shannon.github.mentions import MENTION_LIMIT

pytestmark = pytest.mark.unit

WRITTEN = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
COMMENT = CommentSnapshot(
    repository=REPO,
    item_number=12,
    comment_id=999,
    object_type=ObjectType.ISSUE,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/issues/12#issuecomment-999",
    body="Reproduced on main.",
    author=Actor("monalisa"),
    created_at=WRITTEN,
)
REVIEW = ReviewSnapshot(
    repository=REPO,
    item_number=7,
    review_id=555,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#pullrequestreview-555",
    body="Looks right.",
    state="approved",
    author=Actor("monalisa"),
    created_at=WRITTEN,
)


def said(body: str, mentions: dict[str, int] | None = None, roles: dict[str, int] | None = None):
    return format_comment(replace(COMMENT, body=body), mentions, roles)


class TestANameTheServerKnows:
    def test_a_linked_login_becomes_a_mention(self) -> None:
        assert "<@111>" in said("can you look, @john?", {"john": 111})

    def test_the_case_it_was_written_in_does_not_matter(self) -> None:
        assert "<@111>" in said("thanks @JOHN", {"john": 111})

    def test_a_linked_team_becomes_a_role_mention(self) -> None:
        line = said("cc @canon-regularis/backend", None, {"backend": 900})

        assert "<@&900>" in line

    def test_a_review_body_carries_them_too(self) -> None:
        """The two renderers share `_note`, so testing only one would leave the other resting on
        an implementation detail rather than on a test."""
        assert "<@111>" in format_review(replace(REVIEW, body="nice one @john"), {"john": 111})

    def test_a_name_nobody_linked_is_still_readable(self) -> None:
        line = said("can you look, @nobody?", {"john": 111})

        assert "@nobody" in line
        assert "<@" not in line.split("\n")[1], "somebody was mentioned who was never linked"


class TestNothingTheEscapingDefusedComesBack:
    def test_a_mass_mention_cannot_ping_even_with_that_name_linked(self) -> None:
        """`@everyone` is defused by the escaping before the swap sees it. Linking a GitHub
        account literally called `everyone` is the way that guard would be walked around, so it
        is what the map holds here."""
        line = said("@everyone look at this", {"everyone": 111})

        assert "@everyone" not in line
        assert "<@111>" not in line
        assert "look at this" in line

    def test_a_here_mention_is_refused_the_same_way(self) -> None:
        line = said("@here please", {"here": 111})

        assert "@here" not in line
        assert "<@111>" not in line

    def test_a_user_mention_written_by_hand_cannot_be_re_armed(self) -> None:
        """A login may be all digits, so somebody could link one that matches the id inside a
        `<@…>` a commenter typed. The zero-width space the defusing leaves is what refuses it."""
        line = said("ping <@1234567> about this", {"1234567": 111})

        assert "<@1234567>" not in line
        assert "<@111>" not in line
        assert "1234567" in line

    def test_a_role_mention_written_by_hand_cannot_be_re_armed(self) -> None:
        line = said("ping <@&1234567> about this", None, {"1234567": 900})

        assert "<@&1234567>" not in line
        assert "<@&900>" not in line

    def test_a_login_of_snowflake_length_can_never_be_mentioned_from_a_body(self) -> None:
        """A permanent gap, recorded rather than fixed. GitHub issues logins of digits, and
        `escape_mentions` neutralises a bare run of seventeen to twenty of them before the swap
        is handed the text. It fails in the safe direction, and the way to "fix" it would be to
        stop refusing a zero-width space, which is what keeps every defused mention defused.
        """
        snowflake = "1" * 18

        assert f"<@{snowflake}>" not in said(f"@{snowflake} hello", {snowflake: 111})

    def test_the_author_mention_in_the_header_is_never_rewritten(self) -> None:
        """The header carries a mention this bot built itself, live and never defused, and a
        login may be all digits. The swap is handed the quoted body alone, and the lookbehind
        refusing a leading `<` is the belt to that rule's braces.
        """
        line = said("hello @monalisa", {"monalisa": 7, "7": 111})

        assert line.startswith("**<@7>** commented"), "the bot's own mention was rewritten"
        assert "<@111>" not in line


class TestHowManyOnePersonCanReach:
    def test_it_stops_at_the_limit(self) -> None:
        """Measured before this existed: one comment could put eighty-two live pings in a
        thread, from anybody who can comment on the repository."""
        linked = {f"u{i}": 100 + i for i in range(MENTION_LIMIT + 5)}
        line = said(" ".join(f"@u{i}" for i in range(MENTION_LIMIT + 5)), linked)

        # The author is not in that map, so the header is a plain name and every mention counted
        # here came out of the body.
        assert len(re.findall(r"<@\d+>", line)) == MENTION_LIMIT

    def test_the_names_past_the_limit_are_still_shown(self) -> None:
        """Nothing is hidden. What is lost is the notification, which is what an unlinked name
        already costs."""
        linked = {f"u{i}": 100 + i for i in range(MENTION_LIMIT + 5)}
        line = said(" ".join(f"@u{i}" for i in range(MENTION_LIMIT + 5)), linked)

        assert f"@u{MENTION_LIMIT + 4}" in line

    def test_a_name_written_many_times_does_not_eat_the_budget(self) -> None:
        body = " ".join(["@john"] * 40) + " @octo-cat"

        line = said(body, {"john": 111, "octo-cat": 222})

        assert "<@222>" in line, "a repeated name spent the whole allowance"


class TestTheMessageStillFits:
    def test_a_body_of_nothing_but_names_still_fits_discord(self) -> None:
        """A name is two characters and a mention is twenty-three, so the swap can grow a body
        that was already cut to the preview limit."""
        linked = {f"u{i}": 100000000000000000 + i for i in range(60)}
        line = said("\n".join(f"@u{i}" for i in range(60)), linked)

        assert len(line) <= MESSAGE_LIMIT

    def test_a_mention_is_never_cut_in_half(self) -> None:
        """`fit` drops whole lines, so a mention is either kept or gone. A half-written one would
        render as text rather than ping the wrong person, but it would still be a mess."""
        linked = {f"u{i}": 100000000000000000 + i for i in range(60)}
        line = said("\n".join(f"@u{i}" for i in range(60)), linked)

        for fragment in re.findall(r"<@\d*", line):
            assert fragment + ">" in line, f"a mention was left half written: {fragment!r}"


def test_a_name_inside_backticks_still_resolves() -> None:
    """A divergence from GitHub, written down rather than discovered later. GitHub does not
    resolve a mention inside a code span; the escaping here has already turned the backticks
    into literal text by the time the swap runs, so there is no code span left to respect.
    """
    assert "<@111>" in said("the `@john` variable", {"john": 111})
