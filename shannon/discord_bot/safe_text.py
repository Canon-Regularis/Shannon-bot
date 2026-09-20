"""Making GitHub-authored text safe to put in a Discord message, and short enough to send."""

from __future__ import annotations

import re

import discord

from shannon.domain.text import ZERO_WIDTH_SPACE, lines_within

EMPTY = "None"

MESSAGE_LIMIT = 2000
TRUNCATED = "\n…"

# A comment is a pointer to the discussion on GitHub, not a copy of it.
COMMENT_PREVIEW_LIMIT = 700

# The description an item was opened with. A separate name that shares the comment limit's value.
DESCRIPTION_PREVIEW_LIMIT = 700

# What a commit says about itself under its subject line: a pointer to the commit, not a copy.
COMMIT_MESSAGE_LIMIT = 250

# A commit's subject line. GitHub enforces no length on one. `fit` cannot rescue an over-long
# subject: it drops WHOLE LINES from the end, so the subject would take the statistics line
# below it down with it, leaving half a title and nothing else.
COMMIT_TITLE_LIMIT = 120

# The file an inline review comment sits on, and nothing bounds a path's length. The file and
# line are the FIRST line of that message, so `fit` has nothing it can keep: it falls back to
# cutting characters, and the comment body and the link to GitHub go with it.
REVIEW_PATH_LIMIT = 120

# A CI job's name on a line of its own, beside a link to its log. A name long enough to carry
# that line past the limit takes the link with it, which is the only part worth clicking.
JOB_NAME_LIMIT = 80

# The same name where several are joined onto one line, which holds up to fifteen of them.
JOB_NAME_LIMIT_JOINED = 40


def cut(text: str, *, limit: int) -> str:
    """GitHub-authored text cut to length, before it is made safe.

    Empty for text that is nothing but whitespace, which callers read as a section to leave out
    rather than one to render blank.
    """
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def clipped(body: str, *, limit: int) -> str:
    """GitHub-authored text, cut to length and made safe, with nothing wrapped round it.

    The raw text is cut and only then escaped, never the other way: cutting escaped text looks
    identical, leaves a stray backslash in the thread, and un-escapes whatever followed it.
    """
    text = cut(body, limit=limit)
    if not text:
        return ""
    return as_plain_text(text)


def clipped_path(path: str) -> str:
    """A file path cut to length, and deliberately not escaped.

    This goes inside a code span, where an escape is shown rather than applied, so a backslash in
    a path would arrive doubled and visible. The FRONT goes: a reader wants the file name.
    """
    path = path.strip()
    if len(path) <= REVIEW_PATH_LIMIT:
        return path
    return "…" + path[-(REVIEW_PATH_LIMIT - 1) :]


def clipped_job(name: str, *, limit: int) -> str:
    """A CI job's name cut to length, and unescaped for the reason `clipped_path` gives.

    The FRONT goes: a matrix names its jobs `Tests (Python 3.12)` and `Tests (Python 3.13)`, so
    the end is all that tells them apart.
    """
    name = name.strip()
    if len(name) <= limit:
        return name
    return "…" + name[-(limit - 1) :]


_MENTION = re.compile(r"<(@[!&]?|#)(\d+)>")


def defuse_mentions(text: str) -> str:
    """Stop `<@1234>` resolving; a zero-width space inside the brackets is enough.

    Separate from the markdown escaping because code-span text wants this and not that. A code
    span does not suppress the ping: `allowed_mentions` gates delivery off the raw content.
    """
    return _MENTION.sub("<" + ZERO_WIDTH_SPACE + "\\1\\2>", text)


# `escape_markdown` has one alternative that matches a whole span, `[text](url)`, and it is
# greedy: on a line carrying a link it runs to the last closing parenthesis, backslashes the lot,
# and ships everything between unescaped. Breaking `](` apart stops it matching.
LINK_JOIN = re.compile(r"\]\(")


def as_plain_text(text: str) -> str:
    """Render GitHub-authored text so it displays as written.

    Markup that arrives half-finished, or is cut in two by the preview limit, restyles everything
    after it. `ignore_links=False` overrides a default under which `escape_markdown` skips its URL
    pattern out to the next space, so a comment ending `https://example.com/**` keeps its markers;
    the cost is an escaped, unclickable underscore inside a URL.
    """
    unlinked = LINK_JOIN.sub("]" + ZERO_WIDTH_SPACE + "(", discord.utils.escape_mentions(text))
    return defuse_mentions(discord.utils.escape_markdown(unlinked, ignore_links=False))


_BACKTICK_RUN = re.compile(r"``+")


def code_span(text: str) -> str:
    """Wrap a label in a code span that its own backticks cannot break out of.

    GitHub allows a backtick in a label name, and Discord parts company with the spec on the usual
    answer: three backticks open a code BLOCK, not a longer inline span. So the fence never grows
    past two, and runs inside the text are broken up with a zero-width space instead.
    """
    text = _BACKTICK_RUN.sub(lambda run: ZERO_WIDTH_SPACE.join(run.group(0)), text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space keeps a leading or trailing backtick from merging with the fence. Markdown strips
    # one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def fit(message: str, *, limit: int = MESSAGE_LIMIT) -> str:
    """Trim to a limit on a line boundary.

    Each line is built balanced, so dropping whole lines leaves what remains rendering properly; a
    cut at an arbitrary character can land inside `**bold**` or halfway through a `<@123>` mention.
    The default is Discord's ceiling on a plain message; a panel passes the room its blocks left.
    """
    if len(message) <= limit:
        return message

    budget = limit - len(TRUNCATED)
    kept = lines_within(message, budget)

    # A single line longer than the whole limit has no boundary to cut on.
    if not kept:
        return message[:budget] + TRUNCATED
    return "\n".join(kept) + TRUNCATED
