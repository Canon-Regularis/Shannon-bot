"""Text arithmetic that belongs to neither dialect.

Discord's markdown and GitHub's are different languages, and `discord_bot.safe_text` and
`github.safe_text` keep their rules apart on purpose. What lives here is the counting both need.
"""

from __future__ import annotations

import re

# Used by both dialects to break a construct GitHub or Discord would otherwise act on: a
# mention, a reference, a link join. Named because a mistyped one is invisible in a diff.
ZERO_WIDTH_SPACE = "\u200b"


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
    """
    text = _BACKTICK_RUN.sub(lambda run: ZERO_WIDTH_SPACE.join(run.group(0)), text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space keeps a leading or trailing backtick from merging with the fence. Markdown strips
    # one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"
