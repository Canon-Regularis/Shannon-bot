"""Making GitHub-authored text safe to put in a Discord message, and short enough to send.

Everything here takes a string and returns a string. It knows Discord's limits and what its
markdown will do with hostile input, and nothing about pull requests.
"""

from __future__ import annotations

import re

import discord

EMPTY = "None"

MESSAGE_LIMIT = 2000
TRUNCATED = "\n…"

# A comment is a pointer to the discussion on GitHub, not a copy of it.
COMMENT_PREVIEW_LIMIT = 700


def quote(body: str) -> str:
    """A comment body, made safe to drop into a Discord message.

    Blockquoting alone does not stop GitHub markdown rendering: bold, code fences and mentions
    all still resolve inside a quote. So the text is neutralised first, which also means the
    preview can be cut anywhere without leaving a `**` open and bolding everything after it.
    """
    text = (body or "").strip()
    if not text:
        return ""
    if len(text) > COMMENT_PREVIEW_LIMIT:
        text = text[:COMMENT_PREVIEW_LIMIT].rstrip() + "…"
    return "\n".join(f"> {line}" if line else ">" for line in as_plain_text(text).splitlines())


_MENTION = re.compile(r"<(@[!&]?|#)(\d+)>")


def defuse_mentions(text: str) -> str:
    """Stop `<@1234>` resolving; a zero-width space inside the brackets is enough.

    Separate from the markdown escaping because text going into a code span wants this and not
    that, where backslashes would show. Do not assume the span suppresses the ping either:
    `allowed_mentions` gates delivery off the raw content and honours user mentions.
    """
    return _MENTION.sub("<​\\1\\2>", text)


def as_plain_text(text: str) -> str:
    """Render GitHub-authored text so it displays as written.

    Anyone who can comment on the repository reaches into the thread otherwise: `<@1234>` in a
    body resolves to a real ping, and markup that arrives half-finished, or is cut in two by the
    preview limit, restyles everything after it.

    `ignore_links=False` overrides the default. Left on, `escape_markdown` skips whatever its URL
    pattern matches, and that pattern runs to the next space, so a comment ending
    `https://example.com/**` keeps its markers and takes the rest of the message with it. The
    cost: an underscore in a URL comes out escaped and unclickable inside a quoted body. The
    blocks that quote a body carry an unescaped link of their own, so nothing that matters is
    left unreachable.
    """
    escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(text), ignore_links=False)
    return defuse_mentions(escaped)


def code_span(text: str) -> str:
    """Wrap a label in a code span that its own backticks cannot break out of.

    GitHub allows a backtick in a label name. A single-backtick span around one closes early and
    the rest of the line renders as prose. Markdown's own answer is a longer fence.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space keeps a leading or trailing backtick from touching the fence, which would merge
    # with it. Markdown strips one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def fit(message: str) -> str:
    """Trim to Discord's limit on a line boundary.

    Each line is built balanced, so dropping whole lines leaves what remains rendering properly.
    Cutting at an arbitrary character can land inside `**bold**` or halfway through a `<@123>`
    mention, and the rest of the message goes with it.
    """
    if len(message) <= MESSAGE_LIMIT:
        return message

    budget = MESSAGE_LIMIT - len(TRUNCATED)
    kept: list[str] = []
    used = 0
    # No ordinary exit, so the branch coverage floor is told not to look for one: this is only
    # reached above the limit, the per-line costs sum to exactly the length of the message, and
    # the budget is smaller than that, so a line always crosses it.
    for line in message.split("\n"):  # pragma: no branch
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost

    # A single line longer than the whole limit has no boundary to cut on.
    if not kept:
        return message[:budget] + TRUNCATED
    return "\n".join(kept) + TRUNCATED
