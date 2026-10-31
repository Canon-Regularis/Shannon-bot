"""The start of each line the way GitHub drew it, rather than the way Discord would. Issue #245.

GitHub and Discord read a mark at the start of a line differently, and a thread is where it shows.
GitHub draws a heading in six sizes, in a list item and behind a quote; Discord draws three, never
in a list item, and counts as code only what sits between triple backticks. So each line of a body
is rewritten for Discord to draw a heading where GitHub drew one, and nowhere else:

- a heading keeps the size it was written at, and GitHub's fourth to sixth arrive as the third;
- a heading in a list item arrives bold, because Discord shows a list item's hashes as they are;
- a heading with nothing in it goes, because Discord would make the next line its words;
- a line GitHub never called a heading - indented into code, led by whitespace Discord counts and
  GitHub does not, inside a fence of tildes or a block of HTML - gains a backslash before its hash,
  which Discord shows as the hash;
- a line inside a block of code is left exactly as it was written, whatever it opens with.

The one exception to the last is `-#`, Discord's small print, which is the voice every footnote
this bot writes uses. It is broken on every line, code included: where code starts is decided by
counting backticks, which a link or a bold run can fool, and a footnote forged in the bot's voice
is worse than an invisible character in a line of code.

GitHub's side is read a line at a time, the way GitHub reads it: the quotes and list items a line
carries on or opens, then what it starts inside them, with what each line leaves open carried to
the next - a fence, a block of HTML, a paragraph a tag alone on its line cannot interrupt, a table.

Not `rich_text`'s to know, which is why it is a module of its own: this is about lines, and that is
about a body. What the two share is how a fence is counted, which is `FENCE` below, and how words
put back into a line after it was read are kept words, which is `as_words`.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from enum import Enum, auto

from shannon.domain.text import ZERO_WIDTH_SPACE

# Discord opens a code block on three backticks wherever they appear, including mid-line, and
# `rich_text._balanced` closes one left open by counting them. Counted here the same way, so a line
# this calls code is one Discord draws as code once `_balanced` has run.
FENCE = "```"

# Discord draws three sizes of heading. Its third is bold text at body size, which is how GitHub
# draws a fourth, so GitHub's fourth to sixth arrive as Discord's third rather than as hashes.
HEADING_LEVELS = 3

# What stands in what Discord is shown for a line break inside something taken out of it, such as
# a comment over several lines: a line GitHub read, shown as part of the line before it. A carriage
# return, which no body holds by then, since every line ending is folded to a newline first.
JOINED = "\r"

# JavaScript's whitespace, which is what Discord's own patterns mean by `\s`. Not Python's: it takes
# the byte-order mark, and leaves out the separators U+001C to U+001F and the next-line U+0085.
_DISCORD_SPACES = "".join(
    [
        "\t\n\v\f\r \N{NO-BREAK SPACE}\N{OGHAM SPACE MARK}",
        *map(chr, range(0x2000, 0x200B)),
        "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\N{NARROW NO-BREAK SPACE}",
        "\N{MEDIUM MATHEMATICAL SPACE}\N{IDEOGRAPHIC SPACE}\N{ZERO WIDTH NO-BREAK SPACE}",
    ]
)
_DISCORD_SPACE = f"[{re.escape(_DISCORD_SPACES)}]"

# Everything the opening of a line can be made of. GitHub counts its tabs as columns, at stops of
# four, so the patterns below that read an opening see its tabs laid out as spaces.
_OPENING_CHARACTERS = " \t>*+.)0123456789-"

# One mark opening a quote or a list item, behind up to three spaces: a quote mark and the space
# that belongs to it, or a list mark and the one to four spaces after it - one, where five or more
# open a block of code in the item, and none at the end of the line.
#
# Read one mark at a time, each in one way only. A pattern reading them all at once, where a quote
# mark's space could be its own or the first of the three after it, doubled its work with every
# mark: a line of twenty-two of them took seconds.
_MARK = re.compile(
    r" {0,3}(?:(?P<quote>>) ?|(?:[-*+]|(?P<number>[0-9]{1,9})[.)])(?P<gap> {1,4}(?! )| (?= {4})|$))"
)

# The same quote mark on a line inside a quote that is already open.
_QUOTE_MARK = re.compile(r" {0,3}> ?")

# GitHub's heading, read against what follows a line's quote and list marks and up to three spaces:
# one to six hashes, then a space or a tab and the rest of the line, or nothing at all. Empty words
# are a heading too, with nothing in it.
_HEADING = re.compile(r"(?P<hashes>#{1,6})(?:[ \t](?P<rest>.*))?")

# GitHub's fence opens on three or more backticks or tildes. A backtick one with a backtick after it
# on the line is a code span instead.
_OPENING_FENCE = re.compile(r"(?P<run>`{3,}|~{3,})(?P<info>.*)")

# And closes on a line of the same mark, at least as many of it, up to three spaces in, and nothing
# after it but spaces and tabs - not Python's whitespace, which takes a no-break space as well.
_CLOSING_FENCE = re.compile(r" {0,3}(?P<run>`{3,}+|~{3,}+)[ \t]*+")

# GitHub's blocks of HTML (GFM 4.6), as what opens each kind and what ends it: the line holding the
# end, or for the sixth kind and the seventh below, the blank line after it. Nothing inside one is
# Markdown, so a heading written there is shown as text. The sixth kind is a block-level tag,
# opening or closing; the four before it are a comment, a processing instruction, a declaration
# and a CDATA section. A tag's name is matched in any case, but only in ASCII's: Python's own
# folding would take the long s, U+017F, for an `s`, and end a block GitHub keeps open.
_ANY_CASE = re.IGNORECASE | re.ASCII
_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
    "dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|"
    "header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|"
    "param|section|source|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_HTML_BLOCKS: tuple[tuple[re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        re.compile(r"<(?:script|pre|style|textarea)(?:[ \t\v\f\r>]|$)", _ANY_CASE),
        re.compile(r"</(?:script|pre|style|textarea)>", _ANY_CASE),
    ),
    (re.compile(r"<!--"), re.compile(r"-->")),
    (re.compile(r"<\?"), re.compile(r"\?>")),
    (re.compile(r"<![A-Z]"), re.compile(r">")),
    (re.compile(r"<!\[CDATA\[", _ANY_CASE), re.compile(r"\]\]>")),
    (re.compile(rf"</?(?:{_BLOCK_TAGS})(?:[ \t\v\f\r>]|/>|$)", _ANY_CASE), None),
)

# The seventh kind: one complete tag alone on its line, which GitHub's editor writes for a pasted
# screenshot. It ends at a blank line like the sixth, and cannot interrupt a paragraph.
_TAG_SPACE = r"[ \t\v\f\r]"
_ATTRIBUTE = (
    rf"{_TAG_SPACE}+[A-Za-z_:][A-Za-z0-9_.:-]*"
    rf"(?:{_TAG_SPACE}*={_TAG_SPACE}*(?:[^ \t\v\f\r\"'=<>`]+|'[^']*'|\"[^\"]*\"))?"
)
_TAG_ALONE = re.compile(
    rf"<(?:[A-Za-z][A-Za-z0-9-]*(?:{_ATTRIBUTE})*+{_TAG_SPACE}*/?|/[A-Za-z][A-Za-z0-9-]*"
    rf"{_TAG_SPACE}*)>[ \t\f]*"
)

# A thematic break, which ends a paragraph, and wins over a list mark made of the same characters.
_THEMATIC_BREAK = re.compile(r" {0,3}(?:(?:\*[ \t]*+){3,}|(?:-[ \t]*+){3,}|(?:_[ \t]*+){3,})")

# Under a paragraph, the line of `=` or `-` that makes it a heading GitHub draws and Discord does
# not; and a table's second line, which makes the paragraph line above it the table's header when
# the two have as many cells. Its pipes are optional, and its space is a table's: spaces, tabs,
# vertical tabs and form feeds. Every run is read once: two runs of spaces side by side, each free
# to give the other some back, took over a minute to fail on a line of them ending in a letter.
_UNDERLINE = re.compile(r"(?:=++|-++)[ \t]*+")
_DELIMITER_ROW = re.compile(
    r"\|?[ \t\v\f]*+:?-++:?[ \t\v\f]*+(?:\|[ \t\v\f]*+:?-++:?[ \t\v\f]*+)*+\|?[ \t\v\f]*+"
)

# A pipe between a table's cells, or one with a backslash right before it, which escapes it.
_PIPE_OR_ESCAPE = re.compile(r"\\\||\|")

# Read loosely, for what Discord could still take as a mark: any whitespace - `cut` strips all of it
# from the first line, so Python's, not Discord's - and any quote or list mark, in any order.
_LOOSE_OPENING = r"(?:[^\S\n]|>|(?:[-*+]|[0-9]{1,9}[.)])(?=[^\S\n]))*+"

# A line Discord could draw as a heading: one to three hashes and then its whitespace, or nothing.
_LOOK_ALIKE = re.compile(rf"{_LOOSE_OPENING}(?=#{{1,3}}(?:{_DISCORD_SPACE}|\Z))")

# `-#` is Discord's small print, and GitHub has never heard of it. Broken rather than stripped: on
# GitHub those are two characters somebody meant.
_SUBTEXT = re.compile(rf"{_LOOSE_OPENING}-(?=#)")

# GitHub reads `+` as a bullet and Discord does not, so a list written with one arrives as a
# paragraph of plus signs. `-` and `*` are Discord's own list syntax already, and are left alone.
_PLUS_BULLET = re.compile(r"^([ \t]*)\+([ \t]+)")

# What words put back into a line can open with that makes them something other than words:
# whitespace, Discord's or Python's; a hash; a quote mark; a list mark, or the digits and the `.`
# or `)` of a numbered one, which can also finish a number typed in front of them; or a backtick,
# which can finish a fence out of two typed in front of them.
_OPENS_A_MARK = re.compile(rf"[\s#>*+.)0-9`-]|{_DISCORD_SPACE}")


def lines_as_github_drew_them(written: str, shown: str | None = None) -> str:
    """`shown` with each line opening the way GitHub drew `written`, for Discord to draw the same.

    `shown` is what Discord is to be sent: `written` with what GitHub never shows taken out, such as
    a comment, and the same lines - except that a line break taken out with what held it is a
    `JOINED`, and the lines either side of it are shown as one. GitHub read `written`, so that is
    what is read for where its headings are: a comment opening a line opens a block of HTML, and
    taking it out first would change what GitHub made of every line after it. A line shown is
    opened the way GitHub drew the first line in it. Without `shown`, the two are one.

    Two readings of where code is, because the two disagree. A line Discord shows as code - inside
    a block by the count of backticks before it - is left as shown. A line Discord shows as words
    but GitHub drew as code or as HTML, in a fence of tildes, a fence of backticks Discord split, a
    block of HTML or an indented block, is kept from becoming a heading and nothing more. Every
    other line is read the way GitHub reads it.

    GitHub reads each line as written, form feeds and all. Discord drops them before it reads
    anything - a `-` and a `#` with one between are small print to it - so what is shown is read
    the way Discord will read it.
    """
    lines = written.split("\n")
    if shown is not None and shown.count("\n") + shown.count(JOINED) != len(lines) - 1:
        raise ValueError("what is shown has to hold the lines written, joined or not")
    read = iter(lines)
    said_lines: list[str] = []
    fences = 0
    github = _Reading()
    for shown_line in lines if shown is None else shown.split("\n"):
        drawn = github.read(next(read))
        line = shown_line
        if shown is not None:
            # The lines something taken out ran across: each read as GitHub read it, shown as one.
            for _ in range(shown_line.count(JOINED)):
                github.read(next(read))
            line = shown_line.replace(JOINED, "")
        seen = line.replace("\f", "")
        if fences % 2:
            said: str | None = seen
        elif isinstance(drawn, _Heading) and (rest := _shown_after(drawn, line)) is not None:
            said = _as_drawn(drawn, rest)
        elif drawn is _Drawn.AS_WORDS:
            said = _not_a_heading(_PLUS_BULLET.sub(r"\1-\2", line).replace("\f", ""))
        else:
            said = _not_a_heading(seen)
        if said is not None:
            said_lines.append(_not_subtext(said))
        fences += seen.count(FENCE)
    return "\n".join(said_lines)


def as_words(words: str) -> str:
    """Words put back into a line after the line was read, kept from opening a mark of their own.

    A link to somewhere off GitHub is shown as its words, and a picture lifted out of the text
    leaves its words where it was; either can land at the start of a line, or beside a `-`, a `#`,
    a number or backticks written before it. A zero-width space in front keeps them words, and is
    invisible where it is not needed.
    """
    return ZERO_WIDTH_SPACE + words if _OPENS_A_MARK.match(words) else words


class _Drawn(Enum):
    """How GitHub drew a line that is not a heading."""

    # Inside a fence, a block of HTML or an indented block of code.
    AS_CODE = auto()
    # Anything else, the line opening a fence or a block of HTML included.
    AS_WORDS = auto()


class _Start(Enum):
    """What a line starts that is neither a heading nor a fence nor a block of HTML."""

    BLANK = auto()
    BREAK = auto()
    CODE = auto()


@dataclass(frozen=True, slots=True)
class _Container:
    """A quote, or a list item whose later lines carry on `width` columns further in."""

    width: int = 0


_QUOTE = _Container()


@dataclass(frozen=True, slots=True)
class _Block:
    """A fence or a block of HTML GitHub has open: no line inside it is a heading."""

    # The run of backticks or tildes a fence opened on; empty for HTML.
    fence: str = ""
    # What a block of HTML ends on; None for the kinds that end before a blank line instead.
    end: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class _Heading:
    """A line GitHub drew as a heading: its opening as written, what GitHub found after it, and the
    quotes and list items it sits in."""

    lead: str
    found: re.Match[str]
    quotes: int
    in_an_item: bool


class _Reading:
    """GitHub's reading of a body, a line at a time: the quotes and list items open, innermost last,
    and what the innermost holds open - a fence or a block of HTML, which no line inside is a
    heading in; a paragraph, which a tag alone on its line cannot interrupt; or a table, whose rows
    are not a paragraph."""

    def __init__(self) -> None:
        self.open: list[_Container] = []
        # Where in `open` its quotes are: a blank line carries on the items and ends the first
        # quote, so it finds that quote here rather than by walking past every item to it.
        self.quotes: list[int] = []
        # Whether the innermost container is a list item nothing has been put in yet. A blank line
        # ends one unless it is indented as far as the item: an item begins with at most one
        # blank line (GFM 5.2). Only the innermost can be one, since every other holds the next.
        self.empty = False
        self.block: _Block | None = None
        self.paragraph = False
        self.table = False
        # The paragraph's last line, which a table's second line makes its header; and whether
        # the paragraph was tried as a header already, which GitHub does once.
        self.header = ""
        self.tried = False

    def read(self, written: str) -> _Drawn | _Heading:
        """How GitHub drew `written`, given every line before it."""
        lead = written[: len(written) - len(written.lstrip(_OPENING_CHARACTERS))]
        columns = lead.expandtabs(4) + written[len(lead) :] if "\t" in lead else written
        blank_from = len(columns.rstrip(" \t"))
        matched, at = self._carried_on(columns, blank_from)
        everything = matched == len(self.open)
        if self.block is not None:
            if everything and (self.block.fence or self.block.end is not None or at < blank_from):
                if _ends(self.block, columns, at):
                    self.block = None
                return _Drawn.AS_CODE
            self.block = None
        here = everything and self.paragraph
        new, at = _opened(columns, at, blank_from, interrupting=here)
        start = _start(
            columns,
            at,
            blank=at >= blank_from,
            code=not self.paragraph or bool(new),
            tag=not (here and not new),
        )
        words = columns[at:].lstrip(" ")
        if not everything:
            if self.paragraph and not new and start is None:
                # Words carrying the paragraph on lazily, outside the quote or item it is in, with
                # the space in front of them, which GitHub counts as a table's cell.
                self.header = columns[at:]
                return _Drawn.AS_WORDS
            del self.open[matched:]
            del self.quotes[bisect.bisect_left(self.quotes, matched) :]
            self.paragraph = self.table = False
        if new:
            for container in new:
                if not container.width:
                    self.quotes.append(len(self.open))
                self.open.append(container)
            self.paragraph = self.table = False
            self.empty = bool(new[-1].width) and at >= blank_from
        else:
            self.empty = self.empty and everything and at >= blank_from
        if isinstance(start, re.Match):
            self.paragraph = self.table = False
            quotes = len(self.quotes)
            return _Heading(lead, start, quotes, in_an_item=len(self.open) > quotes)
        if isinstance(start, _Block):
            self.paragraph = self.table = False
            if start.end is None or not start.end.search(columns, at):
                self.block = start
            return _Drawn.AS_WORDS
        if start is not None:
            self.paragraph = self.table = False
            return _Drawn.AS_CODE if start is _Start.CODE else _Drawn.AS_WORDS
        if here and not new:
            # The paragraph carries on, unless the line makes it a heading or a table's header.
            if not columns.startswith("    ", at):
                if _UNDERLINE.fullmatch(words):
                    self.paragraph = False
                elif not self.tried and _DELIMITER_ROW.fullmatch(words):
                    self.tried = True
                    if _cells(words) == _cells(self.header):
                        self.paragraph, self.table = False, True
        elif not self.table or not _cells(words):
            # A paragraph starts, as it does where a table's row has no cells and ends it.
            self.paragraph, self.table, self.tried = True, False, False
        self.header = words
        return _Drawn.AS_WORDS

    def _carried_on(self, columns: str, blank_from: int) -> tuple[int, int]:
        """How many of the open quotes and items `columns` carries on, and where it leaves them: a
        quote by its mark, an item by its width in spaces, or by being blank - except an item
        nothing has been put in yet, which a blank line carries on only when indented as far."""
        at = 0
        for count, container in enumerate(self.open):
            if at >= blank_from:
                later = bisect.bisect_left(self.quotes, count)
                if later < len(self.quotes):
                    return self.quotes[later], at
                if self.empty and len(columns) - at < sum(item.width for item in self.open[count:]):
                    return len(self.open) - 1, at
                return len(self.open), at
            if not container.width:
                mark = _QUOTE_MARK.match(columns, at)
                if mark is None:
                    return count, at
                at = mark.end()
            elif columns.startswith(" " * container.width, at):
                at += container.width
            else:
                return count, at
        return len(self.open), at


def _opened(
    columns: str, at: int, blank_from: int, *, interrupting: bool
) -> tuple[list[_Container], int]:
    """The quotes and list items `columns` opens from `at`, and where its words start after them.

    A list item cannot interrupt a paragraph when it is empty, or numbered from other than one; the
    line carries the paragraph on instead.
    """
    new: list[_Container] = []
    breaks_from = _breaks_from(columns)
    while not (at >= breaks_from and _THEMATIC_BREAK.fullmatch(columns, at)):
        mark = _MARK.match(columns, at)
        if mark is None:
            break
        if mark["quote"]:
            new.append(_QUOTE)
        else:
            empty = mark.end() >= blank_from
            numbered = mark["number"] is not None and int(mark["number"]) != 1
            if interrupting and not new and (empty or numbered):
                break
            # An item with nothing after its mark carries on one column past the mark, however
            # many spaces follow it.
            new.append(_Container(mark.start("gap") - at + 1 if empty else mark.end() - at))
        at = mark.end()
    return new, at


def _breaks_from(columns: str) -> int:
    """The first column a thematic break could start at: where the line's last run of one break
    character, with spaces and tabs between, begins. Tried only from there, a line of a thousand
    list marks is read once rather than once for every mark."""
    end = len(columns.rstrip(" \t"))
    kind = columns[end - 1 : end]
    if kind not in ("*", "-", "_"):
        return len(columns) + 1
    return len(columns[:end].rstrip(kind + " \t"))


def _start(
    columns: str, at: int, *, blank: bool, code: bool, tag: bool
) -> re.Match[str] | _Block | _Start | None:
    """What the words of a line, from `at`, start: a heading, a fence, a block of HTML, a blank
    line, a thematic break or a block of code - or None for words of a paragraph. An indented
    block of code and a tag alone cannot interrupt a paragraph, so `code` and `tag` say whether
    each may start here: true where no paragraph is open for it to interrupt."""
    if blank:
        return _Start.BLANK
    if columns.startswith("    ", at):
        return _Start.CODE if code else None
    if _THEMATIC_BREAK.fullmatch(columns, at):
        return _Start.BREAK
    words = columns[at:].lstrip(" ")
    heading = _HEADING.fullmatch(words)
    if heading is not None:
        return heading
    fence = _OPENING_FENCE.match(words)
    if fence is not None:
        if fence["run"][0] == "`" and "`" in fence["info"]:
            return None
        return _Block(fence=fence["run"])
    for opens, end in _HTML_BLOCKS:
        if opens.match(words):
            return _Block(end=end)
    if tag and _TAG_ALONE.fullmatch(words):
        return _Block()
    return None


def _cells(row: str) -> int:
    """How many cells a table row has, counted as GitHub counts them: what its pipes divide it
    into, a pipe at either end dividing nothing, except that between two pipes with nothing else
    on the row is one empty cell. Space in front is part of the first cell, and a row with no pipe
    is one cell whatever it holds, a vertical tab or a form feed alone included."""
    pipes = [found.start() for found in _PIPE_OR_ESCAPE.finditer(row) if found[0] == "|"]
    if not pipes:
        return 1 if row else 0
    row = row.rstrip(" \t\v\f")
    start = 1 if pipes[0] == 0 else 0
    end = len(row) - 1 if pipes[-1] == len(row) - 1 >= start else len(row)
    return sum(start <= at < end for at in pipes) + 1 if end > start or len(pipes) > 1 else 0


def _ends(block: _Block, columns: str, at: int) -> bool:
    """Whether the line `columns`, inside `block` from `at`, is the last line of it."""
    if block.fence:
        closing = _CLOSING_FENCE.fullmatch(columns, at)
        return (
            closing is not None
            and closing["run"][0] == block.fence[0]
            and len(closing["run"]) >= len(block.fence)
        )
    return block.end is not None and block.end.search(columns, at) is not None


def _shown_after(heading: _Heading, line: str) -> str | None:
    """What `line` shows after the opening GitHub read `heading` by; None where it opens otherwise,
    because the opening was inside something taken out, such as a comment begun on a line above."""
    if heading.found["rest"] is None:
        return ""
    opening = heading.lead + heading.found.string[: heading.found.start("rest")]
    return line[len(opening) :] if line.startswith(opening) else None


def _as_drawn(heading: _Heading, rest: str) -> str | None:
    """A line GitHub drew as a heading, `rest` shown after its opening, for Discord; None for one
    with nothing in it a reader would see - a zero-width space a comment left behind included."""
    words = _closed(rest).replace("\f", "").strip(_DISCORD_SPACES)
    if not words.strip(_DISCORD_SPACES + ZERO_WIDTH_SPACE):
        return None
    if heading.in_an_item or heading.quotes > 1:
        # Discord reads a list item with headings switched off, and does not nest a quote. Words
        # shown as they are rather than bold can still open a quote and a heading of their own -
        # behind the spaces of a line carrying an item on, or after `>>` - so they are kept from
        # it the way a line GitHub drew no heading on is.
        return _not_a_heading(_PLUS_BULLET.sub(r"\1-\2", heading.lead + _bold(words)))
    quoted = "> " if heading.quotes else ""
    level = min(len(heading.found["hashes"]), HEADING_LEVELS)
    return f"{quoted}{'#' * level} {_heading_words(words)}"


def _not_a_heading(line: str) -> str:
    """A backslash before a hash Discord would otherwise draw as a heading. It shows as the hash."""
    found = _LOOK_ALIKE.match(line)
    if found is None:
        return line
    return f"{line[: found.end()]}\\{line[found.end() :]}"


def _not_subtext(line: str) -> str:
    found = _SUBTEXT.match(line)
    if found is None:
        return line
    return f"{line[: found.end()]}{ZERO_WIDTH_SPACE}{line[found.end() :]}"


def _closed(rest: str) -> str:
    """A heading's words without the closing run of hashes GitHub takes off the end, which is one
    only with a space or a tab before it - `# C#` is about C#.

    Read back from the end of the line rather than by a pattern: one trying every place the words
    could stop took seconds over a line of spaces followed by hashes.
    """
    words = rest.rstrip(" \t")
    bare = words.rstrip("#")
    return bare if bare != words and bare[-1:] in ("", " ", "\t") else words


def _heading_words(words: str) -> str:
    """A heading's words. Discord takes any hashes off the end, so one written there is kept."""
    led = _led(words)
    return led + ZERO_WIDTH_SPACE if led.endswith("#") else led


def _bold(words: str) -> str:
    """A list item's heading, as bold. Not where the words hold bold already, or end on a backslash
    that would escape the closing marks: those are shown as they are."""
    if "**" in words or words.endswith("\\"):
        return _led(words)
    return f"**{_led(words)}**"


def _led(words: str) -> str:
    """Words that open on a hash, or on small print, kept from being read as either."""
    if words.startswith("#"):
        return "\\" + words
    if words.startswith("-#"):
        return f"-{ZERO_WIDTH_SPACE}{words[1:]}"
    return words
