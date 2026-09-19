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

# And the same for the description an item was opened with. Its own name at the same number,
# because the two are different things that happen to agree today: a comment is one message in a
# conversation, and this is the standing answer to what the item is for.
DESCRIPTION_PREVIEW_LIMIT = 700

# What a commit says about itself under its subject line, which is a pointer to the commit rather
# than a copy of it. Its own name for the reason the two above give, and the number is the one the
# issue that asked for commit lines named.
COMMIT_MESSAGE_LIMIT = 250

# And its subject line. GitHub enforces no length on one, so without this a single commit written
# by somebody careless is the whole message.
#
# `fit` cannot rescue that, and it is why this limit is here rather than left to it: `fit` drops
# WHOLE LINES from the end, so a subject longer than the message limit would take the statistics
# line underneath it down with it, and what reached the thread would be half a title and nothing
# else. Wider than anything git's own tooling encourages, so no ordinary subject is ever cut.
COMMIT_TITLE_LIMIT = 120

# The file an inline review comment sits on. A path is repository content, and nothing stops one
# being long enough to be the whole message by itself.
#
# Here rather than left to `fit` for the reason above, and more sharply. The file and line are the
# FIRST line of that message and `fit` drops lines from the END, so a path over the limit leaves
# `fit` nothing it can keep: it falls back to cutting characters, and the comment body and the
# link to GitHub both go with it.
REVIEW_PATH_LIMIT = 120

# A CI job's name on a line of its own, beside a link to its log. Issue #112.
#
# Here rather than left to `fit` for the reason the two above give, and with the same failure in
# mind: `fit` drops WHOLE LINES from the end, so a job name long enough to carry its line past the
# limit takes the link to the log with it, which is the only part of a failure worth clicking.
JOB_NAME_LIMIT = 80

# And the same name where several are joined onto one line. Tighter, because that line holds
# fifteen of them and any one of them could otherwise be the whole of it.
JOB_NAME_LIMIT_JOINED = 40


def cut(text: str, *, limit: int) -> str:
    """GitHub-authored text cut to length, and deliberately not yet made safe.

    The half of `clipped` that is only about length. Lifted out because the description takes the
    same cut and then neutralises itself differently since issue #125, and the ORDER is the whole
    of it: the RAW text is cut and only then neutralised, so a cut can never land between a
    backslash or a zero-width space and the character it was protecting.

    Empty for text that is nothing but whitespace, which callers read as a section to leave out
    rather than one to render blank.
    """
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def clipped(body: str, *, limit: int) -> str:
    """GitHub-authored text, cut to length and made safe, with nothing wrapped round it.

    The cut and the escaping in the one order that works, which `cut` above explains. Cutting the
    escaped text instead looks identical, leaves a stray backslash in the thread, and un-escapes
    whatever followed it.

    Empty for text that is nothing but whitespace, which callers read as a section to leave out
    rather than one to render blank.
    """
    text = cut(body, limit=limit)
    if not text:
        return ""
    return as_plain_text(text)


def clipped_path(path: str) -> str:
    """A file path cut to length, and deliberately not escaped.

    Not `clipped`, which escapes markdown. This goes inside a code span, where an escape is shown
    rather than applied, so every backslash in a path would arrive doubled and visible.

    The FRONT is what goes. The end of a path is its file name, which is the part anybody reading
    the line actually wants, and it is the half GitHub keeps in its own interface too.
    """
    path = path.strip()
    if len(path) <= REVIEW_PATH_LIMIT:
        return path
    return "…" + path[-(REVIEW_PATH_LIMIT - 1) :]


def clipped_job(name: str, *, limit: int) -> str:
    """A CI job's name cut to length, and deliberately not escaped.

    Not `clipped`, which escapes markdown: this goes inside a code span, where an escape is shown
    rather than applied, so every backslash would arrive doubled and visible.

    The FRONT is what goes, as it does for a path and for a sharper reason. A matrix names its
    jobs `Tests (Python 3.12)` and `Tests (Python 3.13)`, so everything that tells them apart is
    at the end, and cutting the other way would render a dozen failures as the same line.
    """
    name = name.strip()
    if len(name) <= limit:
        return name
    return "…" + name[-(limit - 1) :]


_MENTION = re.compile(r"<(@[!&]?|#)(\d+)>")


def defuse_mentions(text: str) -> str:
    """Stop `<@1234>` resolving; a zero-width space inside the brackets is enough.

    Separate from the markdown escaping because text going into a code span wants this and not
    that, where backslashes would show. Do not assume the span suppresses the ping either:
    `allowed_mentions` gates delivery off the raw content and honours user mentions.
    """
    return _MENTION.sub("<​\\1\\2>", text)


# `escape_markdown` escapes one character at a time, with one exception: `[text](url)` is an
# alternative in its pattern that matches a whole span. It is greedy, so on a line carrying a
# link it runs from the first bracket to the last closing parenthesis on that line, puts a single
# backslash in front of all of it, and ships everything in between unescaped. Breaking the
# bracket away from the parenthesis is enough to stop it matching, which leaves every marker to
# be escaped individually the way the rest already are.
_LINK_JOIN = re.compile(r"\]\(")


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

    That flag closed one of the two holes. The other is the link alternative above, and it is the
    wider of the two: a pull request titled
    `Fix [regression](https://github.com/o/r/issues/3) in **/*.py (again)` put an odd number of
    bold markers into a metadata block built entirely out of matched pairs, so every label below
    the title paired with the wrong value, and the author of the title chose where that landed.
    A comment could do the same with a code fence and swallow the link back to GitHub with it.
    """
    unlinked = _LINK_JOIN.sub("]​(", discord.utils.escape_mentions(text))
    return defuse_mentions(discord.utils.escape_markdown(unlinked, ignore_links=False))


_BACKTICK_RUN = re.compile(r"``+")


def code_span(text: str) -> str:
    """Wrap a label in a code span that its own backticks cannot break out of.

    GitHub allows a backtick in a label name. A single-backtick span around one closes early and
    the rest of the line renders as prose. Markdown's own answer is a longer fence, and that is
    where Discord parts company with the spec: three backticks there open a code BLOCK, not a
    longer inline span, so a label carrying two of them turned one metadata line into a block,
    and one carrying three closed that block early and left the rest of the message to render as
    whatever came next.

    So the fence never grows past two, and runs inside the text are broken up instead. A
    zero-width space is the same trick the mention defusing uses, and costs a reader nothing.
    """
    text = _BACKTICK_RUN.sub(lambda run: "​".join(run.group(0)), text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space keeps a leading or trailing backtick from touching the fence, which would merge
    # with it. Markdown strips one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def fit(message: str, *, limit: int = MESSAGE_LIMIT) -> str:
    """Trim to a limit on a line boundary.

    Each line is built balanced, so dropping whole lines leaves what remains rendering properly.
    Cutting at an arbitrary character can land inside `**bold**` or halfway through a `<@123>`
    mention, and the rest of the message goes with it.

    `limit` defaults to Discord's ceiling on a plain message, which is what every caller wanted
    when this only trimmed whole messages. Issue #116 gave it a second job: a panel is trimmed by
    dropping whole blocks and then handing this one what room is left, so the same rule about line
    boundaries applies one level down.
    """
    if len(message) <= limit:
        return message

    budget = limit - len(TRUNCATED)
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
