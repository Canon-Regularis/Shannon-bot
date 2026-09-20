"""Text arithmetic that belongs to neither dialect.

Discord's markdown and GitHub's are different languages, and `discord_bot.safe_text` and
`github.safe_text` keep their rules apart on purpose. What lives here is the counting both of
them need and neither of them owns.
"""

from __future__ import annotations

# Used by both dialects to break a construct GitHub or Discord would otherwise act on: a
# mention, a reference, a link join. Named because a mistyped one is invisible in a diff.
ZERO_WIDTH_SPACE = "\u200b"


def lines_within(text: str, budget: int) -> list[str]:
    """The leading whole lines of `text` that fit in `budget`, newlines counted.

    Whole lines, because each line is built balanced on its own: a cut at an arbitrary character
    can land inside `**bold**` or halfway through a `<@123>` mention, and the rest goes with it.

    The caller has already found `text` too long. That precondition is what makes the loop's
    missing ordinary exit true, which the pragma below rests on.
    """
    kept: list[str] = []
    used = 0
    # No ordinary exit, so the branch coverage floor is told not to look for one: the per-line
    # costs sum to exactly the length of the text, and a caller only arrives here with a budget
    # smaller than that, so some line always crosses it.
    for line in text.split("\n"):  # pragma: no branch
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return kept
