"""Text arithmetic that belongs to neither dialect, and the shapes both sides of a seam must share.

Discord's markdown and GitHub's are different languages, and `discord_bot.safe_text` and
`github.safe_text` keep their rules apart on purpose. What lives here is the counting both need,
and the few spellings one package writes and another reads, such as the stored tag token.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# Used by both dialects to break a construct GitHub or Discord would otherwise act on: a
# mention, a reference, a link join. Named because a mistyped one is invisible in a diff.
ZERO_WIDTH_SPACE = "\u200b"

# A masked link, which both dialects spell `[label](url)`. discord.py's own `[.+](.+)` is greedy and
# runs to the last parenthesis on the line; neither class here crosses a bracket, parenthesis, space
# or newline, so a match is one link, and `<` and `>` are out of the address, so no link matched
# here can carry a mention. Here rather than in `discord_bot.rich_text`, which rewrites what it
# matches, since issue #186: the commit finder in `github/` steps over the same links, and has no
# business importing the gateway to do it.
LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()<>\s]*)\)")

# A Discord id. Fifteen to twenty digits is a snowflake: twenty holds 2**64 - 1.
SNOWFLAKE = r"[0-9]{15,20}"

# The token a transcript stores where somebody tagged a person: `discord_bot.capture` WRITES it
# into the row and `github.safe_text` READS it back when the transcript is rendered. One
# definition since #271, because each side had its own copy and its own tests against a literal,
# so either could change shape with every test green while every tagged person went quiet.
TAGGED = re.compile(rf"<@({SNOWFLAKE})>")


def tag(member_id: int) -> str:
    """The stored token for a tag of `member_id`, the one shape `TAGGED` reads back."""
    return f"<@{member_id}>"


def lines_within(text: str, budget: int) -> list[str]:
    """The leading whole lines of `text` that fit in `budget`, newlines counted.

    Whole lines, because a cut at an arbitrary character can land inside `**bold**` or halfway
    through a `<@123>` mention, and the rest of the construct goes with it.
    """
    kept: list[str] = []
    used = 0
    # No ordinary exit, so the branch coverage floor is told not to look for one: the per-line
    # costs sum to exactly the length of the text, and the caller has already found `text` too
    # long for the budget, so some line always crosses it.
    for line in text.split("\n"):  # pragma: no branch
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return kept


_BACKTICK_RUN = re.compile(r"``+")


def code_span(text: str) -> str:
    """Wrap a name somebody typed in a code span that its own backticks cannot break out of.

    GitHub allows a backtick in a label name, and Discord parts company with the spec on the usual
    answer: three backticks open a code BLOCK, not a longer inline span. So the fence never grows
    past two, and runs inside the text are broken up with a zero-width space instead.

    Here rather than in `discord_bot.safe_text`, which imports discord.py: a link that would not
    parse is quoted back by `github/urls.py`, and the parser has no business depending on the
    gateway. Nothing in this is Discord-specific except the fence, and GitHub spells that the
    same way.

    Needs non-empty text: an empty span shows as two bare backticks, so a caller with nothing to
    show writes a sentence without one.
    """
    text = _BACKTICK_RUN.sub(lambda run: ZERO_WIDTH_SPACE.join(run.group(0)), text)
    # #270: after the split no two backticks touch, so the longest run is 0 or 1 and two always
    # closes past it. Written as the cap it is, because three would open a code block.
    fence = "``" if "`" in text else "`"
    # A space keeps a leading or trailing backtick from merging with the fence. Markdown strips
    # one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


# Every run of space and punctuation a team might put between the words of a name. One copy since
# #270: `board` and `priority` each held one, and nothing kept the two alike.
_SEPARATORS = re.compile(r"[\s_\-:/]+")


def normalise(name: str) -> str:
    """Lowercased, each run of separators one space: shared because column names and label names
    are typed by the same people in the same styles, and since #246 a column becomes a label."""
    return _SEPARATORS.sub(" ", name.strip().lower()).strip()


def listed(names: Sequence[str], *, last: str = "and") -> str:
    """`A`, `A and B`, `A, B and C`: a list read as a sentence, its last two joined with `last`.

    Issue #267. `/remind`, `/track` and the workflow commands each wrote this join by hand, so a
    rule such as the Oxford comma could have come to differ between them. No comma before `last`,
    and two names take none at all.
    """
    if len(names) < 3:
        return f" {last} ".join(names)
    return f"{', '.join(names[:-1])} {last} {names[-1]}"
