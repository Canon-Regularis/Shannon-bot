"""The containers a GitHub comment is built from.

Issue #131. These build; `safe_text` next door defuses. The split is the point: every function
here takes text a caller has already made safe, so the escaping rules live in one file instead
of being restated by every construct that has to obey them.

What is worth testing is therefore not "is this escaped" — it is not this module's job — but the
two things a container can get wrong on its own: rendering as something other than the construct
it names, and letting its own contents end it.
"""

from __future__ import annotations

import pytest

from shannon.github.markdown import details, link, note, table

pytestmark = pytest.mark.unit


class TestATable:
    def test_it_carries_the_separator_row_a_table_needs(self) -> None:
        """Without it GitHub renders the pipes as literal text rather than a table."""
        assert table([("Source", "Discord")]).splitlines()[1] == "|---|---|"

    def test_the_name_is_bold_and_the_value_is_not(self) -> None:
        assert "| **Source** | Discord |" in table([("Source", "Discord")])

    def test_rows_keep_the_order_they_were_given(self) -> None:
        built = table([("First", "1"), ("Second", "2")])

        assert built.index("First") < built.index("Second")

    def test_an_empty_value_is_shown_as_nothing_rather_than_a_blank_cell(self) -> None:
        """A cell with nothing in it reads as a row somebody forgot to fill in."""
        assert "| **Channel** | — |" in table([("Channel", "")])

    @pytest.mark.parametrize("cell", [("Name", "a\nb"), ("a\nb", "value")])
    def test_a_line_break_is_refused_rather_than_rendered(self, cell: tuple[str, str]) -> None:
        """It would end the row, and every later cell would shift up a column: the table would
        be rewritten rather than broken, which is worse because it still looks like a table.

        Escaping cannot reach this one. `as_inline_text` handles the pipe, and there is no
        spelling of a newline that stays inside a cell.
        """
        with pytest.raises(ValueError, match="line break"):
            table([cell])


class TestTheFold:
    def test_it_opens_expanded_when_asked(self) -> None:
        assert details("View thread", "x", expanded=True).startswith("<details open>")

    def test_it_opens_shut_when_not(self) -> None:
        built = details("View thread", "x", expanded=False)

        assert built.startswith("<details>")
        assert "<details open>" not in built

    def test_the_body_is_separated_from_the_tags_by_blank_lines(self) -> None:
        """GitHub stops reading markdown inside an HTML block unless a blank line separates
        them, so without these the whole thread renders as one run of literal text."""
        built = details("View thread", "**bold**", expanded=True)

        assert "</summary>\n\n**bold**\n\n</details>" in built

    def test_it_closes_itself(self) -> None:
        assert details("View thread", "x", expanded=True).endswith("</details>")


class TestTheNote:
    def test_it_is_a_github_note_callout(self) -> None:
        assert note("said").startswith("> [!NOTE]\n")

    def test_every_line_is_quoted(self) -> None:
        """One unquoted line ends the callout, and everything below it renders as ordinary text
        while still reading, in the source, as though it were inside."""
        built = note("first\nsecond")

        assert "> first" in built
        assert "> second" in built

    def test_a_blank_line_is_quoted_too_and_left_bare(self) -> None:
        """Quoted, or it ends the callout. Stripped, because `> ` with nothing after it is
        trailing whitespace and the formatter would take it back off."""
        assert note("first\n\nsecond").splitlines()[2] == ">"


class TestALink:
    def test_it_is_a_markdown_link(self) -> None:
        assert link("Shannon", "https://example.com") == "[Shannon](https://example.com)"
