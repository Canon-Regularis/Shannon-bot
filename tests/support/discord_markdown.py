"""How Discord reads the start of a line, spelled out to test this bot's output against. Issue #245.

Not the production rules imported back. A second spelling of what Discord's client does with a
heading and with its small print, so a test can ask what a reader would see rather than what the
code meant to write - and a rule copied from the code under test can only agree with it. Taken from
the markdown rules in Discord's web client, and pinned by `test_how_discord_reads_a_line.py` to the
behaviour those rules encode.

Code is read by the parity of triple backticks before a line, which is how `rich_text._balanced`
decides where the block it may close began. Discord's own block rule agrees with that wherever no
other construct - a link, a bold run, a heading - swallows a fence first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

FENCE = "```"

# JavaScript's `\s`, which is what Discord's patterns mean by whitespace. Not Python's: it takes the
# byte-order mark, and leaves out the separators U+001C to U+001F and the next-line U+0085.
JS_WHITESPACE = (
    "\t\n\v\f\r \N{NO-BREAK SPACE}\N{OGHAM SPACE MARK}\N{EN QUAD}-\N{HAIR SPACE}"
    "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\N{NARROW NO-BREAK SPACE}"
    "\N{MEDIUM MATHEMATICAL SPACE}\N{IDEOGRAPHIC SPACE}\N{ZERO WIDTH NO-BREAK SPACE}"
)
_S = f"[{JS_WHITESPACE}]"

# Discord's heading: one to three hashes, whitespace - which may run into the next line - and words
# that do not open on another run of hashes. A run of hashes at the end is not part of the words.
_HEADING = re.compile(
    rf" *(?P<level>#{{1,3}}){_S}+(?P<words>(?!{_S}*#{{1,3}}{_S})[^\n]+?)#*{_S}*(?:\n|\Z)"
)

# What Discord's `trim` takes off the words.
_TRIMMED = re.compile(rf"{_S}*(?P<words>.*?){_S}*", re.DOTALL)

# A quote: three marks quote the rest of the message, one quotes its line. Either needs a space.
_QUOTE = re.compile(r" *(?:>>> +|>(?!>>) +)")

# Wider than Discord's own subtext rule, which wants `-# ` and a space at the very start of a line:
# this also takes whitespace, quote and list marks in front, and no space after. A test asserting
# that nothing here matches is stricter than Discord needs it to be. A list number is ASCII digits,
# as JavaScript's `\d` is.
_SUBTEXT = re.compile(r"[^\S\n]*(?:>[^\S\n]*|(?:[-*+]|[0-9]{1,9}[.)])[^\S\n]+)*-#")


def _as_discord_sees_it(text: str) -> str:
    """What Discord does to a message before it reads any of it."""
    return re.sub(r"\r\n?", "\n", text).replace("\f", "").replace("\t", "    ")


def headings_drawn(text: str) -> list[tuple[int, str]]:
    """Every heading Discord draws in `text`, as its level and its words, in order.

    A heading has to open a line. One indented under a line of text is not one, because the text
    before it ends on the spaces rather than on the line break; at the very start of a message,
    and under a heading, whose pattern takes its own line break, the indent does not matter. A
    quote's words are read the same way, and a list item's never are - Discord reads list items
    with headings switched off, which the pattern above cannot reach anyway, since a line opening
    on a list mark is not a line opening on a hash.
    """
    text = _as_discord_sees_it(text)
    drawn: list[tuple[int, str]] = []
    starts = [0, *(at + 1 for at, character in enumerate(text) if character == "\n")]
    # Where the last heading drawn outside a quote ended, line break and all.
    after = 0
    for start in starts:
        if start >= len(text) or start < after or text.count(FENCE, 0, start) % 2:
            continue
        heading = None
        if start == after or text[start] != " ":
            heading = _HEADING.match(text, start)
            after = heading.end() if heading is not None else after
        if heading is None:
            quoted = _QUOTE.match(text, start)
            if quoted is not None:
                heading = _HEADING.match(text, quoted.end())
        if heading is not None:
            trimmed = _TRIMMED.fullmatch(heading["words"])
            assert trimmed is not None
            drawn.append((len(heading["level"]), trimmed["words"]))
    return drawn


def subtext_anywhere(text: str) -> list[str]:
    """Every line that opens on `-#`, read more widely than Discord reads one, code included."""
    return [line for line in _as_discord_sees_it(text).split("\n") if _SUBTEXT.match(line)]


def headings_github_drew(lines: Iterable[tuple[str, int | None]]) -> list[int]:
    """The levels a reader should see drawn, in order, for lines whose GitHub reading is known.

    Each line comes with the level GitHub draws it at, or None. One counts unless it falls inside
    code by either reading of where code is: Discord's count of backticks before it, or GitHub's own
    fence - which the strategies using this open and close only with a whole line of three backticks
    or three tildes, an optional language after an opening one.
    """
    levels: list[int] = []
    fences = 0
    fence: str | None = None
    for line, level in lines:
        if level is not None and not fences % 2 and fence is None:
            levels.append(level)
        fences += line.count(FENCE)
        mark = line[:3]
        if mark in (FENCE, "~~~"):
            if fence is None:
                fence = mark
            elif line == fence:
                fence = None
    return levels
