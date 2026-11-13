"""A review asking for changes, as a line in a thread. Issue #263.

What is worth pinning is the part above the review's own words: which files it is on and who made
them, or whom it asks when it cannot say, written so that everybody it rings is visibly named and
so that a message at its longest still has room for what the reviewer actually said.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import (
    FILE_LINES_BUDGET,
    file_lines_width,
    format_review,
)
from shannon.discord_bot.panels import PANEL_BUDGET, BlockKind, Panel
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT, REVIEW_PATH_LIMIT, TRUNCATED
from shannon.domain.models import (
    Actor,
    ChangesAsked,
    FileAuthors,
    RepositorySnapshot,
    ReviewSnapshot,
)
from shannon.domain.text import ZERO_WIDTH_SPACE
from shannon.services.audience import PEOPLE_RUNG

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
SUBMITTED = datetime(2026, 8, 11, 11, tzinfo=UTC)
STAMP = f"<t:{int(SUBMITTED.timestamp())}:f>"

MONALISA = Actor("monalisa", 200)
OCTOCAT = Actor("octocat", 583231)
HUBOT = Actor("hubot", 100)
LINKED = {"monalisa": 606, "hubot": 707, "beedware": 808}

REVIEW = ReviewSnapshot(
    repository=REPO,
    item_number=7,
    review_id=555,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#pullrequestreview-555",
    body="The lock leaks on cancellation.",
    state="changes_requested",
    author=MONALISA,
    created_at=SUBMITTED,
)


def asking(asked: ChangesAsked | None) -> ReviewSnapshot:
    return replace(REVIEW, changes_asked=asked)


def files_of(asked: ChangesAsked, mentions: dict[str, int] | None = None) -> str:
    """The block under the heading, which is where the files and their authors are."""
    blocks = format_review(asking(asked), LINKED if mentions is None else mentions).blocks
    assert blocks[1].kind is BlockKind.FIELDS
    return blocks[1].text


class TestOnTheFiles:
    def test_one_file_says_file_and_author(self) -> None:
        card = format_review(
            asking(ChangesAsked(files=(FileAuthors("README.md", (HUBOT,)),))), LINKED
        )

        assert card.blocks[0].text == f"**<@606>** requested changes on the file {STAMP}"
        assert card.blocks[1].text == "`README.md` made by author <@707>"

    def test_several_files_are_a_line_each(self) -> None:
        """The example in the issue, less the colon: the timestamp follows the verb here."""
        card = format_review(
            asking(
                ChangesAsked(
                    files=(
                        FileAuthors("shannon/services/notes.py", (HUBOT, OCTOCAT)),
                        FileAuthors("README.md", (HUBOT,)),
                    )
                )
            ),
            LINKED,
        )

        assert card.blocks[0].text == f"**<@606>** requested changes on the files {STAMP}"
        assert card.blocks[1].text == (
            "`shannon/services/notes.py` made by authors <@707>, octocat\n"
            "`README.md` made by author <@707>"
        )

    def test_the_files_sit_between_the_heading_and_the_review(self) -> None:
        """Above the body, because a panel over budget drops from the end and these are the names
        it rings with."""
        card = format_review(asking(ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)),))))

        assert [block.kind for block in card.blocks] == [
            BlockKind.HEADING,
            BlockKind.FIELDS,
            BlockKind.BODY,
            BlockKind.FOOTNOTE,
        ]
        assert card.blocks[2].text == "The lock leaks on cancellation."

    def test_authors_left_out_are_counted(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("big.py", (HUBOT, OCTOCAT), more=3),))

        assert files_of(asked) == "`big.py` made by authors <@707>, octocat and 3 more"

    def test_one_name_and_more_still_reads_as_authors(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("big.py", (HUBOT,), more=1),))

        assert files_of(asked) == "`big.py` made by authors <@707> and 1 more"

    def test_a_file_nobody_can_be_named_for_reads_unknown(self) -> None:
        """Every commit on it was a merge or had no account: not nobody, but nobody known."""
        asked = ChangesAsked(files=(FileAuthors("docs/x.md"),))

        assert files_of(asked) == "`docs/x.md` made by Unknown"

    def test_a_file_whose_names_all_went_to_the_count_says_the_count(self) -> None:
        """GitHub said exactly who made it, so it is not unknown."""
        several = ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)), FileAuthors("b.py", more=3)))
        one = ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)), FileAuthors("b.py", more=1)))

        assert files_of(several).splitlines()[1] == "`b.py` made by 3 authors"
        assert files_of(one).splitlines()[1] == "`b.py` made by 1 author"

    def test_files_left_out_are_counted_on_a_line_of_their_own(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)),), more_files=4)

        assert files_of(asked) == "`a.py` made by author <@707>\n-# and 4 more files"

    def test_one_file_left_out_is_one_more_file(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)),), more_files=1)

        assert files_of(asked).splitlines()[1] == "-# and 1 more file"

    def test_a_file_named_beside_one_left_out_makes_files(self) -> None:
        """The heading counts every file the review is on, not only the ones there was room for."""
        card = format_review(
            asking(ChangesAsked(files=(FileAuthors("a.py", (HUBOT,)),), more_files=1))
        )

        assert card.blocks[0].text == f"**monalisa** requested changes on the files {STAMP}"

    def test_an_unlinked_author_is_their_escaped_login(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("a.py", (Actor("snake_case"),)),))

        assert files_of(asked) == "`a.py` made by author snake\\_case"

    def test_nobody_linked_reads_as_plain_logins(self) -> None:
        asked = ChangesAsked(files=(FileAuthors("a.py", (HUBOT, OCTOCAT)),))

        assert files_of(asked, {}) == "`a.py` made by authors hubot, octocat"

    def test_a_path_shaped_like_a_mention_rings_nobody(self) -> None:
        """A path is repository content, named by anybody who can commit."""
        line = files_of(ChangesAsked(files=(FileAuthors("<@1234>.py", (HUBOT,)),)))

        assert "<@1234>" not in line
        assert f"<{ZERO_WIDTH_SPACE}@1234>.py" in line

    def test_a_path_with_a_backtick_stays_inside_its_fence(self) -> None:
        line = files_of(ChangesAsked(files=(FileAuthors("we`ird.py", (HUBOT,)),)))

        assert line == "``we`ird.py`` made by author <@707>"

    def test_a_path_too_long_to_print_keeps_its_file_name(self) -> None:
        line = files_of(ChangesAsked(files=(FileAuthors("a/" * 200 + "module.py", (HUBOT,)),)))
        path = line.split(" made by ")[0]

        assert path.startswith("`…")
        assert path.endswith("module.py`")
        assert len(path) == REVIEW_PATH_LIMIT + 2


class TestFromEveryone:
    def test_everyone_is_named_on_the_heading(self) -> None:
        card = format_review(
            asking(ChangesAsked(everyone=(HUBOT, OCTOCAT, Actor("beedware", 9)))), LINKED
        )

        assert card.blocks[0].text == (
            f"**<@606>** requested changes from <@707>, octocat, <@808> {STAMP}"
        )

    def test_naming_everyone_adds_no_block(self) -> None:
        card = format_review(asking(ChangesAsked(everyone=(HUBOT,))))

        assert [block.kind for block in card.blocks] == [
            BlockKind.HEADING,
            BlockKind.BODY,
            BlockKind.FOOTNOTE,
        ]


class TestAskingNobody:
    def test_a_review_nobody_worked_out_reads_as_it_always_did(self) -> None:
        card = format_review(asking(None))

        assert card.blocks[0].text == f"**monalisa** requested changes {STAMP}"

    def test_asking_nobody_reads_as_the_verdict(self) -> None:
        """Never `requested changes from None`, which is what an empty list of people writes."""
        card = format_review(asking(ChangesAsked()))

        assert card.blocks[0].text == f"**monalisa** requested changes {STAMP}"
        assert BlockKind.FIELDS not in [block.kind for block in card.blocks]


LONGEST_LOGIN = 39


def login(number: int) -> str:
    """A login as long as GitHub allows, never linked, which is the widest a name is written."""
    return f"{number:02d}".ljust(LONGEST_LOGIN, "a")


def at_most(paths: list[str], people: tuple[Actor, ...]) -> ChangesAsked:
    """As many files and names as FILE_LINES_BUDGET holds, the rest counted.

    Most names first, so each line is as long as the budget lets it be. A file that cannot keep
    even one name goes to the count with every file after it.
    """
    kept: list[FileAuthors] = []
    for at, path in enumerate(paths):
        left = len(paths) - at - 1
        shown = range(len(people), 0, -1)
        lines = (FileAuthors(path, people[:named], len(people) - named) for named in shown)
        fitting = [
            line
            for line in lines
            if file_lines_width(ChangesAsked((*kept, line), more_files=left)) <= FILE_LINES_BUDGET
        ]
        if not fitting:
            return ChangesAsked(tuple(kept), more_files=left + 1)
        kept.append(fitting[0])
    return ChangesAsked(tuple(kept))


class TestTheBudget:
    def test_the_measure_is_never_short_of_a_linked_name(self) -> None:
        """Twenty digits is the longest id Discord has, and a login can be far shorter."""
        asked = ChangesAsked(files=(FileAuthors("a.py", (Actor("a"), Actor("b"))),))
        linked = {"a": 2**64 - 1, "b": 2**64 - 1}

        assert file_lines_width(asked) == len(files_of(asked, linked))

    def test_the_measure_is_never_short_of_an_unlinked_name(self) -> None:
        """Escapes included, which is what makes a login wider than it was written."""
        escaped = Actor("_" * LONGEST_LOGIN)
        asked = ChangesAsked(files=(FileAuthors("a.py", (escaped,)),), more_files=2)

        assert file_lines_width(asked) == len(files_of(asked, {}))
        assert file_lines_width(asked) > len("`a.py` made by author ") + LONGEST_LOGIN

    def test_the_longest_file_lines_still_leave_the_review_its_words(self) -> None:
        """Ten files with paths as long as are printed, each made by five people with logins as
        long as GitHub allows, with a reviewer like them and a body cut as long as a note gets.

        A panel over budget drops whole blocks from the end, so lines allowed to run on would cost
        the body, which is what the reviewer is asking for. Nothing at all is dropped here.
        """
        paths = [f"{number:02d}/".ljust(REVIEW_PATH_LIMIT - 3, "p") + ".py" for number in range(10)]
        people = tuple(Actor(login(number)) for number in range(5))
        asked = at_most(paths, people)
        review = replace(
            REVIEW,
            author=Actor(login(99)),
            body="The lock leaks on cancellation. " * 40,
            html_url=(
                f"https://github.com/{'o' * LONGEST_LOGIN}/{'r' * 100}"
                "/pull/99999#pullrequestreview-9999999999"
            ),
            changes_asked=asked,
        )

        card = format_review(review, {})

        assert file_lines_width(asked) > FILE_LINES_BUDGET - REVIEW_PATH_LIMIT * 2
        assert len(card.blocks[2].text) >= COMMENT_PREVIEW_LIMIT
        assert card.length() <= PANEL_BUDGET
        assert card.trimmed() == card
        assert [block.kind for block in card.trimmed().blocks] == [
            BlockKind.HEADING,
            BlockKind.FIELDS,
            BlockKind.BODY,
            BlockKind.FOOTNOTE,
        ]

    def test_a_body_linked_at_its_longest_is_cut_under_the_files_and_never_dropped(self) -> None:
        """The longest file lines, with a body that linking its names and commits makes far longer
        than it was written. The two together run past the budget, and a panel over budget drops
        whole blocks from the end, so the body is cut to what is left before it gets there."""
        paths = [f"{number:02d}/".ljust(REVIEW_PATH_LIMIT - 3, "p") + ".py" for number in range(10)]
        people = tuple(Actor(login(number)) for number in range(5))
        review = longest(changes_asked=at_most(paths, people))

        card = format_review(review, TAGGED)

        assert len(as_written(review)) + card.length() - len(card.blocks[2].text) > PANEL_BUDGET
        assert_the_body_survives(card, [BlockKind.HEADING, BlockKind.FIELDS])

    def test_a_body_linked_at_its_longest_is_cut_under_everyone_and_never_dropped(self) -> None:
        """Everybody one message may ring, each with a login as long as GitHub allows, on the
        heading, which has no budget of its own: the body gives way to them in the same way."""
        everyone = tuple(Actor(login(number)) for number in range(PEOPLE_RUNG))
        review = longest(changes_asked=ChangesAsked(everyone=everyone))

        card = format_review(review, TAGGED)

        assert len(as_written(review)) + card.length() - len(card.blocks[1].text) > PANEL_BUDGET
        assert_the_body_survives(card, [BlockKind.HEADING])


# A repository whose owner and name are as long as GitHub allows, which is what makes the link back
# to the review and every commit linked in its body as long as they get.
LONGEST_REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="o" * LONGEST_LOGIN,
    name="r" * 100,
    html_url=f"https://github.com/{'o' * LONGEST_LOGIN}/{'r' * 100}",
)

# Ten people the body tags, each linked to an id as long as Discord has.
TAGGED = {f"u{number}": 2**64 - 1 for number in range(10)}


def longest(*, changes_asked: ChangesAsked) -> ReviewSnapshot:
    """A review whose body, once linked, is as long as a note's body gets.

    Cut to COMMENT_PREVIEW_LIMIT as written, then grown by everything linking adds: ten tagged
    names, five commits, and markup that every character of is escaped.
    """
    names = " ".join(f"@u{number}" for number in range(10))
    commits = " ".join(["abcdef1"] * 5)
    return replace(
        REVIEW,
        repository=LONGEST_REPO,
        author=Actor(login(99)),
        body=f"{names} {commits} {'*_' * COMMENT_PREVIEW_LIMIT}"[:COMMENT_PREVIEW_LIMIT],
        html_url=f"{LONGEST_REPO.html_url}/pull/99999#pullrequestreview-99999999999",
        changes_asked=changes_asked,
    )


def as_written(review: ReviewSnapshot) -> str:
    """The body as it is linked, before anything cuts it to the room the names leave."""
    blocks = format_review(replace(review, changes_asked=None), TAGGED).blocks
    assert blocks[1].kind is BlockKind.BODY
    return blocks[1].text


def assert_the_body_survives(card: Panel, above: list[BlockKind]) -> None:
    """Nothing is dropped, and the body still opens with the first person it tags."""
    assert card.length() <= PANEL_BUDGET
    assert card.trimmed() == card
    assert [block.kind for block in card.blocks] == [*above, BlockKind.BODY, BlockKind.FOOTNOTE]
    assert card.blocks[len(above)].text.startswith(f"<@{2**64 - 1}>")
    assert card.blocks[len(above)].text.endswith(TRUNCATED)
