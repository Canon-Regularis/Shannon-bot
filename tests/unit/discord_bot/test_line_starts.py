"""Each line of a body, opened the way GitHub drew it rather than the way Discord would. Issue #245.

The tables are the cases where GitHub and Discord disagree about the same characters; the
properties at the bottom ask the test-side spelling of Discord's rules what a reader would see, so
they hold the pass to what is drawn rather than to what it meant to write.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shannon.discord_bot.line_starts import (
    FENCE,
    HEADING_LEVELS,
    JOINED,
    as_words,
    lines_as_github_drew_them,
)
from tests.support.discord_markdown import (
    JS_WHITESPACE,
    headings_drawn,
    headings_github_drew,
    subtext_anywhere,
)

pytestmark = pytest.mark.unit

ZWSP = "\N{ZERO WIDTH SPACE}"
NBSP = "\N{NO-BREAK SPACE}"
BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"

# Whitespace to Discord that GitHub does not take for a space after a heading's hashes. A form feed
# is left out: Discord drops it before reading anything. `JS_WHITESPACE` is spelled as a character
# class, ranges and all, so it is read as one.
ONLY_DISCORD_SPACES = [
    space
    for space in map(chr, range(0x10000))
    if re.fullmatch(f"[{JS_WHITESPACE}]", space) and space not in " \t\n\r\f"
]


def drawn(text: str) -> str:
    return lines_as_github_drew_them(text)


def named(space: str) -> str:
    return f"U+{ord(space):04X}"


class TestAHeadingIsAHeading:
    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("# Title", "# Title"),
            ("## Language-Specific Time Multipliers", "## Language-Specific Time Multipliers"),
            ("### Detail", "### Detail"),
            ("#### Four", "### Four"),
            ("###### Six", "### Six"),
            ("   ## Three spaces", "## Three spaces"),
            ("#\tTabbed", "# Tabbed"),
            ("## Closed ##", "## Closed"),
            ("# x\t#", "# x"),
            ("# x #\t", "# x"),
            ("# a\fb", "# ab"),
            ("# a\tb", "# a\tb"),
            ("# C#", f"# C#{ZWSP}"),
            (f"# C#{BOM}", f"# C#{ZWSP}"),
            ("# foo \\#", f"# foo \\#{ZWSP}"),
            ("# # nested", "# \\# nested"),
            ("# #5 fixed", "# \\#5 fixed"),
            (f"# {NBSP}## x", "# \\## x"),
            (f"# {BOM}## x", "# \\## x"),
            ("## -# x", f"## -{ZWSP}# x"),
        ],
        ids=[
            "one hash",
            "two, from the issue",
            "three",
            "four is drawn as the third",
            "six as well",
            "indented up to three spaces",
            "a tab after the hashes",
            "a closing run",
            "a closing run behind a tab",
            "a tab after a closing run",
            "a form feed in the words, which Discord drops",
            "a tab in the words, left as it is",
            "a hash ending the words",
            "a hash ending them before a byte-order mark",
            "an escaped hash ending them",
            "words opening on a hash",
            "words opening on a reference",
            "words behind a no-break space",
            "words behind a byte-order mark",
            "words opening on small print",
        ],
    )
    def test_it_is_drawn_at_the_size_it_was_written(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    def test_discord_draws_each_at_the_level_it_was_written(self) -> None:
        """The issue's own body, read back the way Discord reads it."""
        body = (
            "Possible workarounds include the following:\n\n"
            "## Language-Specific Time Multipliers\n"
            "- C / C++ (base):  `1.0x`\n\n"
            "## CPU Time vs Wall-Clock Time\n"
            "`Isolate` can be used to track actual CPU time.\n\n"
            "#### Reference-Based Normalization\n"
            "**Note:** This is the most time-excessive way of doing this."
        )

        assert headings_drawn(drawn(body)) == [
            (2, "Language-Specific Time Multipliers"),
            (2, "CPU Time vs Wall-Clock Time"),
            (3, "Reference-Based Normalization"),
        ]

    def test_a_hash_ending_the_words_is_still_there_to_read(self) -> None:
        assert headings_drawn(drawn("# C#")) == [(1, f"C#{ZWSP}")]

    def test_three_is_the_number_discord_draws(self) -> None:
        assert HEADING_LEVELS == 3


class TestAHeadingWithNothingInIt:
    """Gone, line and all: Discord would make whatever comes next the heading's words."""

    @pytest.mark.parametrize("written", ["#", "##   ", "# #", "## ###", "- #", "> > ## ##"])
    def test_the_line_goes(self, written: str) -> None:
        assert drawn(written) == ""

    @pytest.mark.parametrize("space", [*ONLY_DISCORD_SPACES, "\f"], ids=named)
    def test_nothing_but_whitespace_discord_counts_is_nothing(self, space: str) -> None:
        """Discord's whitespace, not Python's: a byte-order mark is nothing to Discord, and to
        Python it is a character."""
        assert drawn(f"# {space}\nnext") == "next"

    def test_the_lines_around_it_stay(self) -> None:
        assert drawn("intro\n#\nnext line") == "intro\nnext line"

    def test_the_next_line_is_not_made_a_heading(self) -> None:
        assert headings_drawn(drawn("intro\n#\nnext line")) == []


class TestWhatGitHubDoesNotCallAHeading:
    @pytest.mark.parametrize(
        "written",
        [
            "#3 is fixed by this",
            "#hashtag",
            "\\# escaped",
            "####### Seven",
            f"####{NBSP}x",
            "1234567890. # x",
            "#\x1cx",
            "a\N{LINE SEPARATOR}# b",
        ],
        ids=[
            "an issue reference",
            "no space",
            "escaped",
            "seven hashes",
            "four hashes and a no-break space, which Discord draws nothing for either",
            "a number too long to be a list's",
            "a separator Discord does not count as whitespace",
            "a line separator Discord does not break on",
        ],
    )
    def test_it_is_left_as_written(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("    ## Four spaces", "    \\## Four spaces"),
            ("\t## Tab", "\t\\## Tab"),
            (f"{NBSP}# lead", f"{NBSP}\\# lead"),
            ("    #\ntext", "    \\#\ntext"),
            ("-     # five spaces", "-     \\# five spaces"),
            (">     # code in a quote", ">     \\# code in a quote"),
            (">\t  # x", ">\t  \\# x"),
            ("   >\t # x", "   >\t \\# x"),
            ("-\t\t# x", "-\t\t\\# x"),
            ("1.\t   # x", "1.\t   \\# x"),
            ("\f# x", "\\# x"),
            ("#\f x", "\\# x"),
            (">\f# x", ">\\# x"),
            ("- \f# x", "- \\# x"),
            ("> a\n    > # x", "> a\n    > \\# x"),
            ("> ~~~\n   > # x\n> ~~~", "> ~~~\n   > \\# x\n> ~~~"),
            ("   * * *\n     # x", "   * * *\n     \\# x"),
        ],
        ids=[
            "four spaces in",
            "a tab in",
            "a no-break space in front",
            "a bare hash four spaces in",
            "five spaces after a list mark",
            "five spaces after a quote mark",
            "a tab and two spaces after a quote mark",
            "a tab after a quote mark at its fourth column",
            "two tabs after a list mark",
            "a tab and three spaces after a number",
            "a form feed in front",
            "a form feed after the hash",
            "a form feed in a quote",
            "a form feed in a list item",
            "a quote mark four spaces in, carrying a quote's paragraph on",
            "a quote mark three spaces in, inside its quote's fence",
            "five spaces in, under a thematic break three spaces in",
        ],
    )
    def test_one_discord_would_draw_shows_its_hash_instead(self, written: str, shown: str) -> None:
        """A backslash in front, which Discord shows as the hash itself. GitHub counts a tab to
        the next stop of four, and a form feed as no space at all; Discord drops the form feed."""
        assert drawn(written) == shown

    @pytest.mark.parametrize("space", ONLY_DISCORD_SPACES, ids=named)
    def test_whitespace_only_discord_counts_does_not_make_one(self, space: str) -> None:
        assert drawn(f"#{space}x") == f"\\#{space}x"

    @pytest.mark.parametrize(
        "written",
        ["    ## Four spaces", "\t## Tab", f"#{NBSP}nbsp", f"{NBSP}# lead", "    #\ntext"],
    )
    def test_discord_draws_none_of_them_even_first_in_a_body(self, written: str) -> None:
        """`cut` strips the first line of a body, which is what used to let them through."""
        assert headings_drawn(drawn(written)) == []
        assert headings_drawn(drawn(written).strip()) == []


class TestInAListItem:
    """Discord reads a list item with headings switched off, so GitHub's heading arrives bold - on
    the line with the item's mark, and on any line carrying the item on."""

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("- ## Step one", "- **Step one**"),
            ("1. ### x", "1. **x**"),
            ("+ ## plus item", "- **plus item**"),
            ("* # C#", "* **C#**"),
            ("- ## #5", "- **\\#5**"),
            ("- ## **Note** this", "- **Note** this"),
            ("- ## trailing\\", "- trailing\\"),
            ("> - ## item", "> - **item**"),
            ("-\t# x", "-\t**x**"),
            ("- a\n  # x", "- a\n  **x**"),
            ("* # Z\n   # F", "* **Z**\n   **F**"),
            ("- a\n\n  # x", "- a\n\n  **x**"),
            ("- a\n    - ## b", "- a\n    - **b**"),
            ("-\t\n  # x", "-\t\n  **x**"),
            ("1.  \n   # x", "1.  \n   **x**"),
            ("-     # code\n  # Summary", "-     \\# code\n  **Summary**"),
            ("- a\nb\n  # c", "- a\nb\n  **c**"),
            ("> - a\n>   # x", "> - a\n>   **x**"),
            ("text\n1. # x", "text\n1. **x**"),
            ("> a\n2. # b", "> a\n2. **b**"),
            ("1) # x", "1) **x**"),
            ("123456789. # x", "123456789. **x**"),
            ("-    # x", "-    **x**"),
            ("-\n  # x", "-\n  **x**"),
            ("-\n  \n  # x", "-\n  \n  **x**"),
            ("-\n  a\n\n  # x", "-\n  a\n\n  **x**"),
            ("> a\n- b\n  # x", "> a\n- b\n  **x**"),
            ("text\n> 2. # x", "text\n> 2. **x**"),
            ("- > # x", "- > **x**"),
            (
                "| a | b |\n|---|---|\n- x\n  <img src=x>\n  # x",
                "| a | b |\n|---|---|\n- x\n  <img src=x>\n  **x**",
            ),
        ],
        ids=[
            "a bullet",
            "a number",
            "a plus, which becomes a bullet",
            "a hash ending the words",
            "words opening on a reference",
            "words holding bold already",
            "words ending on a backslash",
            "in a quote",
            "a tab after the mark",
            "a line carrying the item on",
            "and carrying it on further in",
            "after a blank line",
            "a nested item four spaces in",
            "under an item with nothing after its mark",
            "under a numbered one",
            "under an item opening on code",
            "after a line carrying it on lazily",
            "carried on inside a quote",
            "an item numbered one, under a paragraph",
            "any number, outside the quote a paragraph is in",
            "a number closed by a bracket",
            "a number of nine digits",
            "four spaces after the mark",
            "under an item with nothing after its mark, at the end of the line",
            "under an empty item and a blank line indented as far as it",
            "under an item given its words on the next line, past a blank line",
            "in an item opened outside a quote's paragraph",
            "any number, in a quote opened under a paragraph",
            "in a quote inside an item",
            "in an item under a table, past a tag alone that cannot interrupt its paragraph",
        ],
    )
    def test_it_arrives_bold(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            (
                "- Steps\n  # > # **Posted by the maintainers**",
                "- Steps\n  > \\# **Posted by the maintainers**",
            ),
            (
                "- Steps\n  # > # Posted by the maintainers\\",
                "- Steps\n  > \\# Posted by the maintainers\\",
            ),
            (">># > # **Posted by the maintainers**", ">>> \\# **Posted by the maintainers**"),
        ],
        ids=[
            "words holding bold, on a line carrying the item on",
            "words ending on a backslash",
            "words under two quote marks, which the quote mark in them would make three",
        ],
    )
    def test_words_shown_as_they_are_open_no_quoted_heading(self, written: str, shown: str) -> None:
        """Where bold would break the words they are shown as they are, and a quote mark opening
        them must not become a quote holding a heading GitHub drew none of."""
        assert drawn(written) == shown
        assert headings_drawn(shown) == []

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("- a\n # x", "- a\n# x"),
            ("- - -\n  # x", "- - -\n# x"),
            ("* * *\n  # x", "* * *\n# x"),
            ("text\n-\n  # x", "text\n-\n# x"),
            ("-\n # x", "-\n# x"),
            ("-\n\n  # x", "-\n\n# x"),
            ("-\n \n  # x", "-\n \n# x"),
            ("-\n\n\n  # x", "-\n\n\n# x"),
            ("- a\n-\n\n  # x", "- a\n-\n\n# x"),
            (
                "Steps:\n\n1.\n\n   ## Install\n   pip install shannon",
                "Steps:\n\n1.\n\n## Install\n   pip install shannon",
            ),
            ("> 1.\n>\n>    ## x", "> 1.\n>\n> ## x"),
            ("> -\n>\n>   # x", "> -\n>\n> # x"),
            ("> - a\n> # x", "> - a\n> # x"),
        ],
        ids=[
            "not far enough in to carry the item on",
            "under a thematic break of dashes, which is no list",
            "or of stars",
            "under a dash underlining the paragraph above, which opens no item",
            "not far enough in for an item with nothing after its mark",
            "after a blank line, which ends an item nothing was put in",
            "and a blank line not indented as far as it",
            "and two blank lines",
            "and an empty item after another",
            "in numbered steps",
            "in a quote",
            "in a quote, under a dash",
            "in the quote, outside its item",
        ],
    )
    def test_one_outside_any_item_is_a_heading(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    @pytest.mark.parametrize("number", ["2", "0"])
    def test_an_item_numbered_from_other_than_one_cannot_interrupt_a_paragraph(
        self, number: str
    ) -> None:
        """So the line is the paragraph's words, and its hash is kept from Discord's list."""
        assert drawn(f"text\n{number}. # x") == f"text\n{number}. \\# x"


class TestInAQuote:
    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("> ## Quoted", "> ## Quoted"),
            (">## Tight", "> ## Tight"),
            (" > #### Deep", "> ### Deep"),
            ("> > ## Nested", "> > **Nested**"),
            (">\t# x", "> # x"),
            ("> \t# x", "> # x"),
            ("> a\n>    # x", "> a\n> # x"),
            ("   > # x", "> # x"),
        ],
        ids=[
            "spaced",
            "tight",
            "indented and deep",
            "nested, which Discord does not draw",
            "a tab after the mark",
            "a space and a tab",
            "carried on, three spaces in",
            "its mark three spaces in",
        ],
    )
    def test_it_is_a_heading_inside_the_quote(self, written: str, shown: str) -> None:
        assert drawn(written) == shown


class TestCodeIsShownAsWritten:
    @pytest.mark.parametrize(
        "written",
        [
            "```diff\n+ added\n- removed\n```",
            "```bash\n# install deps\npip install -r requirements.txt\n```",
            "```suggestion\n# keep this comment\n+ return None\n```",
            "```\ncode\n```\n## After",
            "``` a ` b ```\n# x",
        ],
        ids=[
            "a diff keeps its plus lines",
            "a shell comment keeps its hash",
            "a suggestion keeps both",
            "a heading after the block is still one",
            "a code span is not a fence",
        ],
    )
    def test_it_is_left_exactly_as_written(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        ("written", "shown"),
        [("```a```\n#\nnext", "```a```\nnext"), ("``` a ` b ```\n#### x", "``` a ` b ```\n### x")],
        ids=["an empty heading after a block on one line", "a heading after one"],
    )
    def test_every_fence_on_a_line_is_counted(self, written: str, shown: str) -> None:
        """Two on one line open a block and close it, so the line after is words again."""
        assert drawn(written) == shown

    def test_backticks_are_counted_as_discord_reads_them(self) -> None:
        """A form feed between them is gone before Discord counts, so they open a block."""
        assert drawn("``\f`\n#### x") == "```\n#### x"

    def test_a_fence_opened_mid_line_holds_what_follows(self) -> None:
        """Discord opens a block wherever three backticks are, and `_balanced` closes it."""
        assert drawn("#### Use ``` here\n#### after") == "### Use ``` here\n#### after"

    def test_a_longer_fence_holding_a_shorter_one_is_code_to_github(self) -> None:
        """Discord pairs the inner backticks and shows the line between as words."""
        assert drawn("````\n```\n# between\n```\n````") == "````\n```\n\\# between\n```\n````"


class TestAFenceGitHubDrew:
    def test_its_lines_are_code_to_github_and_kept_from_being_headings(self) -> None:
        """Discord has no fence of tildes, so the lines inside are words there: no heading is made
        of them, and nothing else about them changes."""
        written = "~~~bash\n# install\n+ x\n~~~\n## After"

        assert drawn(written) == "~~~bash\n\\# install\n+ x\n~~~\n## After"

    @pytest.mark.parametrize(
        "written",
        [
            "~~~\ncode\n~~~~\n## After",
            "~~~\ncode\n~~~\t\n## After",
            "~~~\ncode\n   ~~~\n## After",
            "> ~~~\n> code\n\n## Heading",
            "> ~~~\n\n> # x",
            "- ~~~\n  code\n# Heading",
        ],
        ids=[
            "on a longer run",
            "on one with a tab after it",
            "on one three spaces in",
            "with the quote it is in",
            "with its quote at a blank line, before the next quote",
            "with the list item it is in",
        ],
    )
    def test_it_ends_where_github_ends_it(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("~~~\ncode\n    ~~~\n## Still code\n~~~", "~~~\ncode\n    ~~~\n\\## Still code\n~~~"),
            ("~~~\n> ~~~\n## x\n~~~", "~~~\n> ~~~\n\\## x\n~~~"),
            ("~~~\n- ~~~\n## x\n~~~", "~~~\n- ~~~\n\\## x\n~~~"),
            (f"~~~\n~~~{NBSP}\n## x\n~~~", f"~~~\n~~~{NBSP}\n\\## x\n~~~"),
            ("~~~\n~~~\v\n## x\n~~~", "~~~\n~~~\v\n\\## x\n~~~"),
            ("~~~\n~~~\f\n## x\n~~~", "~~~\n~~~\n\\## x\n~~~"),
            ("~~~~\n~~~\n## x\n~~~~", "~~~~\n~~~\n\\## x\n~~~~"),
            ("~~~\n~~~ x\n## y\n~~~", "~~~\n~~~ x\n\\## y\n~~~"),
            ("~~~\n\n# x\n~~~", "~~~\n\n\\# x\n~~~"),
            ("> ~~~\n> # x\n> ~~~", "> ~~~\n> \\# x\n> ~~~"),
            ("- ~~~\n  # x\n  ~~~", "- ~~~\n  \\# x\n  ~~~"),
            ("- a\n  ~~~\n  # x\n## After", "- a\n  ~~~\n  \\# x\n## After"),
            ("~~~ a`b\n# x\n~~~", "~~~ a`b\n\\# x\n~~~"),
            ("-\n\n  ~~~\n# x\n~~~", "-\n\n  ~~~\n\\# x\n~~~"),
            ("> -\n>\n>   ~~~\n> # x\n> ~~~", "> -\n>\n>   ~~~\n> \\# x\n> ~~~"),
        ],
        ids=[
            "on a run four spaces in",
            "on a run behind a quote mark",
            "on a run behind a list mark",
            "on a run with a no-break space after it",
            "on a run with a vertical tab after it",
            "on a run with a form feed after it",
            "on a shorter run",
            "on a run with words after it",
            "across a blank line",
            "inside the quote it is in",
            "inside the list item it is in",
            "inside an item, until the item ends",
            "of tildes, its words holding a backtick",
            "opened after an empty item that a blank line ended",
            "the same in a quote",
        ],
    )
    def test_it_holds_what_github_holds(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("    ~~~\n\n## Next", "    ~~~\n\n## Next"),
            ("\t~~~\n\n## Next", "\t~~~\n\n## Next"),
            ("\f~~~\n\n## Next", "~~~\n\n## Next"),
            ("-     ~~~\n\n## Next", "-     ~~~\n\n## Next"),
            ("``\n# x", "``\n# x"),
        ],
        ids=[
            "four spaces in",
            "a tab in",
            "behind a form feed",
            "five spaces after a list mark",
            "two backticks",
        ],
    )
    def test_a_run_github_does_not_call_a_fence_opens_nothing(
        self, written: str, shown: str
    ) -> None:
        assert drawn(written) == shown


class TestABlockOfHTML:
    """GFM 4.6: a line opening on HTML can open a block of it, and nothing inside is Markdown - a
    heading written there is text on GitHub. GitHub's editor writes one for a pasted screenshot."""

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            (
                "<pre>\n# systemctl status shannon\nActive: failed\n</pre>",
                "<pre>\n\\# systemctl status shannon\nActive: failed\n</pre>",
            ),
            (
                "<details>\n<summary>Environment</summary>\n## OS\nWindows 11\n</details>",
                "<details>\n<summary>Environment</summary>\n\\## OS\nWindows 11\n</details>",
            ),
            ("</details>\n## Additional context", "</details>\n\\## Additional context"),
            (
                '<img width="1024" alt="image" src="https://github.com/user-attachments/a">\n'
                "### Expected behaviour",
                '<img width="1024" alt="image" src="https://github.com/user-attachments/a">\n'
                "\\### Expected behaviour",
            ),
            ("<!-- Describe the change\n## Summary", "<!-- Describe the change\n\\## Summary"),
            ("<?php\n# x\n?>", "<?php\n\\# x\n?>"),
            ("<!A\n# x", "<!A\n\\# x"),
            ("<![CDATA[\n# x\n]]>", "<![CDATA[\n\\# x\n]]>"),
            ("<div\n# x", "<div\n\\# x"),
            ("<img src=x/>\n# x", "<img src=x/>\n\\# x"),
            ("<IMG SRC=X>\n# x", "<IMG SRC=X>\n\\# x"),
            ("> <div>\n> # x", "> <div>\n> \\# x"),
            ("- <div>\n  # x", "- <div>\n  \\# x"),
            ("<div>\n>\n# x", "<div>\n>\n\\# x"),
            ("<div>\n\f\n# x", "<div>\n\n\\# x"),
            ("<pre>\n\n# x\n</pre>", "<pre>\n\n\\# x\n</pre>"),
            ('<pre class="log">\n# x\n</pre>\n# y', '<pre class="log">\n\\# x\n</pre>\n# y'),
            ("text\n<details>\n## x", "text\n<details>\n\\## x"),
            (
                "<script>\n</\N{LATIN SMALL LETTER LONG S}cript>\n# x\n</script>",
                "<script>\n</\N{LATIN SMALL LETTER LONG S}cript>\n\\# x\n</script>",
            ),
            ("<style>\n# x\n</style>\n# y", "<style>\n\\# x\n</style>\n# y"),
            ("<PRE>\n# x\n</PRE>\n# y", "<PRE>\n\\# x\n</PRE>\n# y"),
            ("<pre\v>\n\n# x\n</pre>", "<pre\v>\n\n\\# x\n</pre>"),
            ("> <!A\n> x\n> # y", "> <!A\n> x\n> \\# y"),
            ("> <!A x\n> # y", "> <!A x\n> \\# y"),
            ("<![CData[\n# x\n]]>\n# y", "<![CData[\n\\# x\n]]>\n# y"),
            ("<img src=x>\f\n# x", "<img src=x>\n\\# x"),
            ("text\n- <img src=x>\n  # x", "text\n- <img src=x>\n  \\# x"),
            ("text\n-     code\n  <img src=x>\n  # x", "text\n-     code\n  <img src=x>\n  \\# x"),
        ],
        ids=[
            "a shell session in pre",
            "details written without a blank line",
            "a closing tag",
            "a pasted screenshot",
            "a comment never closed",
            "a processing instruction",
            "a declaration",
            "a CDATA section",
            "a block tag at the end of its line",
            "a tag closing itself",
            "a tag in capitals",
            "in a quote",
            "in a list item",
            "a quote mark inside, which is HTML too",
            "a form feed, which is not a blank line",
            "a blank line, which does not end one that ends on its end tag",
            "pre with attributes, up to its end tag",
            "a block tag, which can interrupt a paragraph",
            "past an end tag GitHub does not read, spelled with a letter Python folds to s",
            "style, up to its end tag",
            "pre in capitals, up to its end tag in capitals",
            "pre with a vertical tab after its name, across a blank line",
            "a declaration in a quote, whose end is not the quote's own mark",
            "the same, opened with words on its line",
            "a CDATA section in mixed case",
            "a tag alone with a form feed after it",
            "a tag alone opening an item, which holds no paragraph yet",
            "a tag alone under code that opened an item, which holds no paragraph either",
        ],
    )
    def test_a_heading_inside_one_is_shown_as_text(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    @pytest.mark.parametrize(
        "opening",
        [
            "text\n<p>",
            "text\n<table>",
            "text\n<h2>",
            "text\n<summary>",
            "text\n<ul>",
            "text\n<DIV>",
            "text\n</div>",
            "<div/>more",
            "text\n<div\v>",
            "<pre",
            "<?",
            "<![cdata[",
            "<a b='c'>",
            '<a data-x="1">',
            '<a b ="c">',
            '<a b= "c">',
            "<my-tag>",
            '<img\vsrc="x">',
            "</a >",
            "<img src=x> ",
            "<br/>",
            "1)\n\n   <details>",
            "-\n\n  <div>",
        ],
        ids=[
            "p, interrupting a paragraph",
            "table",
            "a heading's own tag",
            "summary",
            "ul",
            "a block tag in capitals",
            "a closing block tag",
            "a block tag closing itself, words after it",
            "a block tag with a vertical tab after its name",
            "pre at the end of its line",
            "a processing instruction with nothing after it",
            "a CDATA section in lower case",
            "a tag alone with a single-quoted value",
            "with an attribute name holding a dash",
            "with a space before the equals sign",
            "with a space after it",
            "a tag name holding a dash",
            "a vertical tab between the name and an attribute",
            "a closing tag with a space before its bracket",
            "a space after the bracket",
            "a tag closing itself",
            "after an empty numbered item a blank line ended",
            "after an empty item a blank line ended",
        ],
    )
    def test_each_kind_holds_the_heading_under_it(self, opening: str) -> None:
        assert drawn(f"{opening}\n# x") == f"{opening}\n\\# x"

    @pytest.mark.parametrize(
        "written",
        [
            "<pre>\n\\# x\n</pre>\n# y",
            "<pre>x</pre>\n# y",
            "<details>\n\n## OS",
            "<!DOCTYPE html>\n# x",
            "<![CDATA[\n]]>\n# y",
            "<script>\n</SCRIPT>\n# y",
            "<textarea>\n</textarea>\n# y",
            "> <div>\n# x",
            "> <div>\n>\n> # x",
            "- <div>\n# x",
            "<details>\n~~~\n</details>\n\n## Next",
            "<pre>\n```\n# x\n```\n</pre>\n# y",
            "<style>\n</style>\n# x",
            "<pre>\nx</pre>\n# y",
            "<!-- a\n-->\n# x",
            "<?php\n?>\n# x",
        ],
        ids=[
            "on its end tag",
            "on the line it opened on",
            "at a blank line",
            "a declaration, on its own line",
            "a CDATA section, on its end",
            "a script, on its end in capitals",
            "a text area, on its end",
            "with the quote it is in",
            "at a blank line in the quote",
            "with the list item it is in",
            "with a fence mark inside it opening nothing",
            "with code inside it",
            "style, on its end tag",
            "pre, on an end tag after words",
            "a comment, on its end",
            "a processing instruction, on its end",
        ],
    )
    def test_it_ends_where_github_ends_it(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        "written",
        [
            "<divx\n# x",
            "<a b='c'd>\n# x",
            "<a / >\n# x",
            "</a b>\n# x",
            "<a b=`c`>\n# x",
            "<img\nsrc=x>\n# x",
            "<img src=x> trailing\n# x",
            "<!a\n# x",
            "<d\N{LATIN SMALL LETTER DOTLESS I}v>\n# x",
            "<!-x\n# y",
        ],
        ids=[
            "a tag name that only starts like a block tag",
            "an attribute run into a quoted value",
            "a slash apart from its bracket",
            "a closing tag with an attribute",
            "a value in backticks",
            "a tag split over two lines",
            "a tag with words after it",
            "a declaration in lower case",
            "a block tag spelled with a letter Python folds to i",
            "a comment opened with one dash",
        ],
    )
    def test_a_line_that_is_not_a_tag_alone_opens_nothing(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        "written",
        [
            "Screenshot:\n<img src=x>\n### Expected behaviour",
            "- a\n  <img src=x>\n# x",
            "text\n    x\n<img src=x>\n# y",
            "text\n<span>\n# x",
            "text\n**\n<img src=x>\n# x",
            "a | b\n> |---|---|\n> <img src=x>\n> # x",
            "> | a | b |\n> |---|---|\nc\n<img src=x>\n# x",
            "a\n|---|---|\n| b | c |\n|---|---|\n<img src=x>\n# x",
            "a | b\n|-|\nc | d\n|-|-|\n<img src=x>\n# x",
            "> a\n | b | c |\n> |---|---|\n> <img src=x>\n> # x",
            "| a |\n|---|\n|\n<img src=x>\n# x",
            "| a |\n|---|\n| b |\n|\n<br>\n# x",
            "text\n--\nmore\n<img src=x>\n# x",
        ],
        ids=[
            "under a paragraph",
            "under one in a list item",
            "under a paragraph's indented line",
            "a tag GitHub does not count as a block's",
            "under two stars, which are words",
            "under a table's second line in a quote the paragraph is not in",
            "a paragraph that ended a quoted table",
            "a paragraph GitHub tried once as a table's header",
            "the same, with a header under the first try",
            "a header whose leading space is a cell of its own",
            "a row with no cells, which ends a table",
            "the same after a row with one",
            "a paragraph after two dashes, which underline the one above and make no table",
        ],
    )
    def test_a_tag_alone_cannot_interrupt_a_paragraph(self, written: str) -> None:
        assert drawn(written) == written

    @pytest.mark.parametrize(
        "before",
        [
            "> text",
            "- a",
            "text\n---",
            "text\n===",
            "text\n=== ",
            "text\n--",
            "---",
            "text\n___",
            "text\n_\t_\t_",
            "    x",
            "text\n# h",
            "text\n<pre>x</pre>",
            "text\n~~~\n~~~",
            "| a | b |\n|---|---|",
            "| a | b |\n|---|---|\n| c | d |",
            "x\n| a | b |\n|---|---|",
            "a | b\n|---|---|",
            "| a | b |\n|:--|--:|",
            "| a | b |\n|-|-|",
            "a | b\n--- | ---",
            "a | b\n|--- | ---",
            "| a | b |\t\n|---|---|",
            "a\n:--",
            "Status\n---:",
            "a\n-:",
            "a \\\\| b | c\n|---|---|",
            "| a | b |\n|---|\v---|",
            "||\n|---|",
            "a | b\n|-|\n\nc | d\n|-|-|",
            "| a |\n|---|\n\v",
            "\v\n|-|",
        ],
        ids=[
            "outside the quote the paragraph is in",
            "outside the list item it is in",
            "under a line of dashes that made it a heading",
            "under a line of equals signs that did",
            "the same with a space after them",
            "under two dashes",
            "under a thematic break",
            "under one of underscores",
            "under one of underscores and tabs",
            "under an indented block of code",
            "under a heading",
            "under a block of HTML that ended on its own line",
            "under a fence",
            "under a table, which is not one",
            "under a table's rows, which are not one either",
            "under a table whose header ended a paragraph",
            "under a table written without its outer pipes",
            "under a table with its cells aligned",
            "under one of single dashes",
            "under one with no pipe at either end of either line",
            "under one whose second line opens on a pipe and the first does not",
            "under one whose header ends on a tab",
            "under a table of one column, written without pipes",
            "under one aligned right",
            "under one of a single dash",
            "under one whose escaped pipe has a backslash before its backslash",
            "under one with a vertical tab in its second line",
            "under one whose header is one empty cell",
            "under a table in the paragraph after one GitHub tried as a header",
            "under a row of only a vertical tab, which is a cell and carries the table on",
            "under a table whose header is only a vertical tab",
        ],
    )
    def test_it_can_where_no_paragraph_is_open(self, before: str) -> None:
        assert drawn(f"{before}\n<img src=x>\n# x") == f"{before}\n<img src=x>\n\\# x"

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            (
                "> a\nb | c\n> |---|---|\n> <img src=x>\n> # x",
                "> a\nb | c\n> |---|---|\n> <img src=x>\n> \\# x",
            ),
            ("> a\n> ---:\n> <img src=x>\n> # x", "> a\n> ---:\n> <img src=x>\n> \\# x"),
            (
                "| a | b |\n|---|---|\f\n<img src=x>\n# x",
                "| a | b |\n|---|---|\n<img src=x>\n\\# x",
            ),
            ("| a |\n|---|\n\f\n<img src=x>\n# x", "| a |\n|---|\n\n<img src=x>\n\\# x"),
            (
                "> a\n  b | c\n> |---|---|\n> <img src=x>\n> # x",
                "> a\n  b | c\n> |---|---|\n> <img src=x>\n> \\# x",
            ),
        ],
        ids=[
            "in a quote, under a table whose header carried its paragraph on lazily",
            "in a quote, under a table of one column",
            "under a table whose second line ends on a form feed",
            "under a row of only a form feed, which is a cell",
            "in a quote, under a header carried on lazily two spaces in, with no pipe at its end",
        ],
    )
    def test_it_can_in_a_quote_and_past_a_form_feed(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    @pytest.mark.parametrize(
        "written",
        [
            "| a |\n|---|---|\n<img src=x>\n# x",
            "a \\| b\n|---|---|\n<img src=x>\n# x",
            "| a | b |\n    |---|---|\n<img src=x>\n# x",
            "|\n|---|\n<img src=x>\n# x",
            "a \\\\| b\n|---|---|\n<img src=x>\n# x",
        ],
        ids=[
            "fewer cells above",
            "an escaped pipe, which divides nothing",
            "a second line in code",
            "a first line of one pipe, which has no cells",
            "a pipe escaped behind another backslash, so one cell above",
        ],
    )
    def test_a_table_needs_as_many_cells_on_both_lines(self, written: str) -> None:
        """Without them there is no table, and the paragraph goes on."""
        assert drawn(written) == written


class TestSubtextIsAlwaysBroken:
    """`-#` is the voice every footnote this bot writes uses, and it is broken everywhere."""

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("-# posted by the maintainers", f"-{ZWSP}# posted by the maintainers"),
            ("  -# indented", f"  -{ZWSP}# indented"),
            (f"{NBSP}-# forged", f"{NBSP}-{ZWSP}# forged"),
            ("> -# quoted", f"> -{ZWSP}# quoted"),
            ("- -# listed", f"- -{ZWSP}# listed"),
            ("-\f# x", f"-{ZWSP}# x"),
            ("12. -# x", f"12. -{ZWSP}# x"),
            ("1) -# x", f"1) -{ZWSP}# x"),
            ("```c\n-#include <a.h>\n```", f"```c\n-{ZWSP}#include <a.h>\n```"),
            ("```\n-\f# x\n```", f"```\n-{ZWSP}# x\n```"),
            ("<pre>\n-# x", f"<pre>\n-{ZWSP}# x"),
            (
                "lgtm https://example.com/```\n-# posted",
                f"lgtm https://example.com/```\n-{ZWSP}# posted",
            ),
        ],
        ids=[
            "alone",
            "indented",
            "behind a no-break space",
            "in a quote",
            "in a list item",
            "with a form feed Discord drops",
            "behind a number of two digits",
            "behind a number closed by a bracket",
            "even in code",
            "in code, behind a form feed Discord drops",
            "even in HTML",
            "after a fence a link would swallow",
        ],
    )
    def test_it_is_broken(self, written: str, shown: str) -> None:
        assert drawn(written) == shown


class TestPlusBullets:
    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("+ one\n+ two", "- one\n- two"),
            ("  + nested", "  - nested"),
            ("+not a bullet", "+not a bullet"),
            ("+ ~~~\n  + x\n  ~~~", "- ~~~\n  + x\n  ~~~"),
            ("+\f x", "+ x"),
            ("+\tone\n+\ttwo", "-\tone\n-\ttwo"),
            ("- a\n\t+ b", "- a\n\t- b"),
        ],
        ids=[
            "a list",
            "nested",
            "no space",
            "an item opening a fence, but not the code inside it",
            "a form feed, which is no space to GitHub",
            "a tab after the plus",
            "nested a tab in",
        ],
    )
    def test_github_s_bullet_becomes_discord_s(self, written: str, shown: str) -> None:
        assert drawn(written) == shown

    def test_one_in_an_indented_block_of_code_is_code(self) -> None:
        assert drawn("text\n\n    + x") == "text\n\n    + x"


class TestWordsThatCannotOpenAMark:
    @pytest.mark.parametrize(
        "words",
        [
            "# x",
            "-# x",
            "> x",
            " x",
            "-x",
            "* -# x",
            "+ -# x",
            "1. -# x",
            ". -# x",
            ") -# x",
            "2024 roadmap",
            "0x",
            "9x",
            f"{BOM}x",
            "\vx",
            "\x1cx",
            "`x",
        ],
        ids=[
            "a hash",
            "small print",
            "a quote mark",
            "a space",
            "a dash",
            "a bullet",
            "a plus",
            "a number",
            "the dot of a number typed before them",
            "or its bracket",
            "a digit",
            "a zero",
            "a nine",
            "a byte-order mark, which only Discord calls whitespace",
            "a vertical tab",
            "a separator only Python calls whitespace",
            "a backtick, which two typed before them make a fence",
        ],
    )
    def test_words_opening_on_a_mark_are_kept_words(self, words: str) -> None:
        assert as_words(words) == ZWSP + words

    @pytest.mark.parametrize("words", ["x", "@alice", "", "click here", "_under_", "[x]"])
    def test_any_other_words_are_left_alone(self, words: str) -> None:
        assert as_words(words) == words


class TestWhatIsShownIsNotWhatWasRead:
    """What Discord is sent can have things GitHub never shows taken out of it - a comment, a
    picture - line for line. GitHub read the body with them in, so where its headings are is read
    from what was written, and what comes out is what is shown."""

    @pytest.mark.parametrize(
        ("written", "shown", "said"),
        [
            ("- <!-- c -->\n\n  # x", "- \n\n  # x", "- \n\n  **x**"),
            (
                "<!-- c --> words\n<img src=x>\n# x",
                " words\n<img src=x>\n# x",
                " words\n<img src=x>\n\\# x",
            ),
            ("<details>\n<!-- c -->\n# x", "<details>\n\n# x", "<details>\n\n\\# x"),
            (
                "-<!-- --> text\n  ~~~\n# x\n~~~",
                "- text\n  ~~~\n# x\n~~~",
                "- text\n  ~~~\n\\# x\n~~~",
            ),
        ],
        ids=[
            "an item holding a comment is not empty, and a blank line does not end it",
            "a comment opening a line opens a block of HTML, and a tag alone can follow it",
            "a comment alone on a line is not a blank line ending a block of HTML",
            "a dash glued to a comment is not a list mark",
        ],
    )
    def test_where_github_drew_headings_is_read_from_what_was_written(
        self, written: str, shown: str, said: str
    ) -> None:
        assert lines_as_github_drew_them(written, shown) == said

    def test_a_heading_s_words_are_the_ones_shown(self) -> None:
        assert lines_as_github_drew_them("## Title <!-- c -->", "## Title ") == "## Title"

    def test_a_heading_with_nothing_shown_in_it_goes(self) -> None:
        said = lines_as_github_drew_them("words\n# <!-- c -->\nmore", "words\n# \nmore")

        assert said == "words\nmore"

    def test_so_does_one_left_holding_a_zero_width_space(self) -> None:
        """What a comment taken out from in front of a closing run leaves: nobody would see it."""
        said = lines_as_github_drew_them("words\n# <!-- --> #\nmore", f"words\n# {ZWSP} #\nmore")

        assert said == "words\nmore"

    def test_one_whose_opening_is_not_shown_is_shown_as_words(self) -> None:
        """A heading is drawn only from a line that still opens the way GitHub read it."""
        said = lines_as_github_drew_them("a <!--\n# b --> # c", "a \n # c")

        assert said == "a \n \\# c"
        assert headings_drawn(said) == []

    def test_lines_joined_are_read_one_by_one_and_shown_as_one(self) -> None:
        """A comment over two lines of a quote, taken out: the words after it stay in the quote."""
        said = lines_as_github_drew_them("> a <!-- x\n> y --> b", f"> a {JOINED} b")

        assert said == "> a  b"

    def test_a_line_joined_on_is_still_read(self) -> None:
        """A fence opened inside a comment begun mid-line holds the line after it, on GitHub."""
        said = lines_as_github_drew_them("a <!--\n~~~ -->\n# x\n~~~", f"a {JOINED}\n# x\n~~~")

        assert said == "a \n\\# x\n~~~"
        assert headings_drawn(said) == []

    def test_a_line_shown_is_opened_the_way_github_drew_its_first(self) -> None:
        """A heading with a comment begun in it: the line after it is read, and joined to it."""
        said = lines_as_github_drew_them("# Title <!-- a\nnote -->", f"# Title {JOINED}")

        assert said == "# Title"
        assert headings_drawn(said) == [(1, "Title")]

    def test_discord_s_count_of_fences_is_of_the_lines_joined(self) -> None:
        """Two backticks, a comment over two lines, and one more: one fence once joined."""
        said = lines_as_github_drew_them(
            "```\n``<!--\n-->`\n# x\n```", f"```\n``{JOINED}`\n# x\n```"
        )

        assert said == "```\n```\n\\# x\n```"
        assert headings_drawn(said) == []

    def test_a_bullet_is_github_s_where_it_is_shown(self) -> None:
        assert lines_as_github_drew_them("+ <!-- c -->x", "+ x") == "- x"

    def test_discord_s_count_of_fences_is_of_what_is_shown(self) -> None:
        """Taking out what stood between two runs of backticks can make a fence of them."""
        said = lines_as_github_drew_them("```\n``<!-- -->`\n# x\n```", "```\n```\n# x\n```")

        assert said == "```\n```\n\\# x\n```"
        assert headings_drawn(said) == []

    @pytest.mark.parametrize("shown", ["one", f"one{JOINED}two{JOINED}three", "one\ntwo\nthree"])
    def test_what_is_shown_has_the_lines_that_were_written(self, shown: str) -> None:
        with pytest.raises(ValueError, match="lines written"):
            lines_as_github_drew_them("one\ntwo", shown)


_GAP = " " * 100_000
_RUN = "#" * 100_000


class TestALineWrittenToBeSlow:
    """Every line of a body is read before anything cuts it, so a line written to make the reading
    try every way of reading it holds up everything else the bot is doing. Each of these took longer
    the longer it was - twenty-two quote marks took seconds, and twice as long with one more - and
    is read at once now. Should that come back, these stop finishing: the suite's timeout fails them
    in CI, and on Windows, where a timeout cannot stop a pattern partway, the run hangs instead."""

    @pytest.mark.parametrize(
        ("written", "shown"),
        [
            ("> " * 5000 + "x", "> " * 5000 + "x"),
            (">  " * 5000 + "x", ">  " * 5000 + "x"),
            ("- > " * 5000 + "x", "- > " * 5000 + "x"),
            ("> " * 5000 + "# x", "> " * 5000 + "**x**"),
            (f"# a{_GAP}{_RUN}x", f"# a{_GAP}{_RUN}x"),
            (f"# a{_GAP}{_RUN}", "# a"),
            ("- " * 20_000 + "x" + "\n" * 20_000, "- " * 20_000 + "x" + "\n" * 20_000),
            ("- " * 100_000 + "*", "- " * 100_000 + "*"),
            ("<a" + " b" * 30_000, "<a" + " b" * 30_000),
            ("a\n|-" + " " * 300_000 + "x", "a\n|-" + " " * 300_000 + "x"),
        ],
        ids=[
            "quote marks and no hash",
            "quote marks with two spaces",
            "list and quote marks",
            "quote marks and a heading",
            "words, spaces, hashes and more words",
            "words, spaces and a closing run",
            "list marks, then blank lines that carry every item on",
            "list marks ending on a star, which could be a thematic break",
            "a tag that never closes",
            "a table's second line under a paragraph, its spaces ending on a letter",
        ],
    )
    def test_it_is_read_at_once(self, written: str, shown: str) -> None:
        assert drawn(written) == shown


# Lines whose GitHub reading is known, as (line, the level GitHub draws it at, or nothing). A
# heading is indented at most one space: two or more under a list item carry the item on, and make
# the heading its own.
_WORDS = st.sampled_from(["Summary", "C#", "#5 fixed", "Fixed in abc1234", "a **bold** one", "x"])
_HEADING_LINES = st.builds(
    lambda indent, hashes, gap, words, closing: (
        " " * indent + "#" * hashes + gap + words + closing,
        min(hashes, HEADING_LEVELS),
    ),
    st.integers(min_value=0, max_value=1),
    st.integers(min_value=1, max_value=6),
    st.sampled_from([" ", "\t", "  "]),
    _WORDS,
    st.sampled_from(["", " #", " ###"]),
)
_QUOTED_LINES = st.builds(
    lambda quote, hashes, words: (f"{quote}{'#' * hashes} {words}", min(hashes, HEADING_LEVELS)),
    st.sampled_from(["> ", ">", " > ", ">\t"]),
    st.integers(min_value=1, max_value=6),
    _WORDS,
)
_OTHER_LINES = st.sampled_from(
    [
        (line, None)
        for line in [
            "#3 is fixed",
            "#hashtag",
            "\\# escaped",
            "####### Seven",
            "    ## code",
            "\t## tab",
            f"#{NBSP}x",
            f"#{BOM}x",
            f"{NBSP}# lead",
            "-     # code",
            ">     # code",
            ">\t  # code",
            "-\t\t# code",
            "\f# fed",
            "#\f fed",
            "- ## Step one",
            "1. ### x",
            "> > ## Nested",
            "#",
            "## ",
            "# #",
            "-# small",
            "words",
            "",
            "+ plus",
        ]
    ]
)
_FENCE_LINES = st.sampled_from([("```", None), ("```py", None), ("~~~", None), ("~~~bash", None)])
_PIECES = st.lists(
    st.one_of(_HEADING_LINES, _QUOTED_LINES, _OTHER_LINES, _FENCE_LINES), max_size=24
)


@given(_PIECES)
@settings(max_examples=400)
def test_discord_draws_a_heading_exactly_where_github_drew_one(
    pieces: list[tuple[str, int | None]],
) -> None:
    """At the level GitHub drew it, in order, and nowhere else - read by Discord's rules."""
    body = "\n".join(line for line, _ in pieces)

    shown = drawn(body)

    assert [level for level, _ in headings_drawn(shown)] == headings_github_drew(pieces)
    assert [level for level, _ in headings_drawn(shown.strip())] == headings_github_drew(pieces), (
        "the first line, once `cut` strips it, became a heading GitHub never drew"
    )


_CODE = st.lists(
    st.sampled_from(
        ["# install", "## x", "+ added", "- removed", "-#include <a.h>", "#### f", "    # x", "x"]
    ),
    max_size=8,
)


@given(_CODE, st.sampled_from(["", "py", "diff", "bash"]))
@settings(max_examples=200)
def test_a_block_of_code_is_shown_as_it_was_written(code: list[str], language: str) -> None:
    """Every line inside, whatever it opens with, apart from small print, which is always broken."""
    written = "\n".join(code)
    kept = "\n".join(f"-{ZWSP}{line[1:]}" if line.startswith("-#") else line for line in code)

    shown = drawn(f"intro\n```{language}\n{written}\n```\n#### After")

    assert shown == f"intro\n```{language}\n{kept}\n```\n### After"


_SOUP = st.lists(
    st.sampled_from(
        ["-", "#", "-#", " ", "\t", NBSP, ">", "> ", "- ", "1. ", "\n", "```", "~~~", "x", "\f"]
    ),
    max_size=60,
).map("".join)


@given(st.one_of(_SOUP, st.text(max_size=200)))
@settings(max_examples=400)
def test_no_line_ever_opens_on_small_print(text: str) -> None:
    """Code included, and read more widely than Discord itself reads one."""
    folded = text.replace("\r\n", "\n").replace("\r", "\n")

    assert subtext_anywhere(drawn(folded)) == []


@given(
    st.one_of(
        _SOUP.map(lambda text: text.replace("\f", "")),
        _PIECES.map(lambda pieces: "\n".join(line for line, _ in pieces)),
    )
)
@settings(max_examples=400)
def test_reading_it_twice_changes_nothing_more(text: str) -> None:
    """Over bodies without a form feed in a soup: GitHub reads one that the pass drops for
    Discord, so a second reading of what the pass shows reads another body - four hashes and a
    form feed are words to GitHub, and four hashes alone a heading with nothing in it."""
    shown = drawn(text)

    assert drawn(shown) == shown
    assert shown.count(FENCE) == text.count(FENCE)
    assert shown.count("**") % 2 == text.count("**") % 2
