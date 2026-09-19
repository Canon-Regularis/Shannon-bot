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


def clipped(body: str, *, limit: int) -> str:
    """GitHub-authored text, cut to length and made safe, with nothing wrapped round it.

    Lifted out of `quote` below rather than written twice, because the order is the whole of it:
    the RAW text is cut and only then escaped, so a cut can never land between a backslash and the
    character it was protecting. Cutting the escaped text instead looks identical, leaves a stray
    backslash in the thread, and un-escapes whatever followed it.

    Empty for text that is nothing but whitespace, which callers read as a section to leave out
    rather than one to render blank.
    """
    text = (body or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
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


def quote(body: str, *, limit: int = COMMENT_PREVIEW_LIMIT) -> str:
    """A comment body, made safe to drop into a Discord message.

    Blockquoting alone does not stop GitHub markdown rendering: bold, code fences and mentions
    all still resolve inside a quote. So the text is neutralised first, which also means the
    preview can be cut anywhere without leaving a `**` open and bolding everything after it.
    """
    text = clipped(body, limit=limit)
    if not text:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


# GitHub's web form submits CRLF, and every rule below is anchored to a line. Folded first so
# the rest of them see one kind of line ending, and so a blank line costs one character against
# the preview limit rather than two.
_LINE_ENDINGS = re.compile(r"\r\n?")

# Invisible on GitHub and very much not here. A pull request template opens with one of these
# and carries more between its sections, so without this the preview of a templated repository
# is the instructions to the author rather than anything the author wrote.
#
# Non-greedy, so two comments do not merge into one match and swallow the description between
# them. An unterminated `<!--` matches nothing and is left as written, which is the safe way
# round: the alternative eats the rest of the body.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# A space after the hashes is required, and that is the whole of what makes this safe. Without
# it the rule also strips a line-leading issue reference, so a description beginning `#3 is
# fixed by this` came out as `3 is fixed by this` with the reference gone. GitHub wants the
# space for a heading anyway, so demanding it is the more correct reading as well as the safer.
_HEADING = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)

# The marker only. What was quoted stays, and it ends up inside the quote this is going into.
_QUOTED = re.compile(r"^>+[ \t]*", re.MULTILINE)

# `[ \t]*` rather than `\s*`, which is the bug this rule exists to avoid rather than a detail of
# it: `\s` matches a newline, so with MULTILINE the match reaches back over the blank line above
# and the escape lands at the end of the previous line. That is exactly what `escape_markdown`
# does to a list today, and why one bullet comes out escaped and the next does not.
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)

# Anything but a bullet Discord will not escape back. `-`, `*` and `+` are all markdown to it,
# so writing one of those here means `escape_markdown` puts the backslash straight back on.
_BULLET_MARK = "• "

_BLANK_RUN = re.compile(r"\n{3,}")

_MENTION = re.compile(r"<(@[!&]?|#)(\d+)>")


def as_prose(text: str) -> str:
    """GitHub markdown with its structure flattened to something that reads as prose.

    Nothing here makes text safe. It runs before the escaping, never instead of it, and what
    comes out still goes through `as_plain_text` like any other GitHub-authored string. That
    ordering is what lets this be as simple as it is: removing a comment can join whatever sat
    either side of it, and the escaping downstream neutralises the result either way.

    It exists because the escaping is the thing that makes a description unreadable. Discord's
    escaper puts a backslash in front of a line-leading `#` and a line-leading `-`, so a perfectly
    ordinary description comes out as a wall of backslashes with a stray one on a line of its own.
    Flattening the structure first leaves nothing for it to escape, and a heading reads as a line
    and a list reads as a list.

    What it does not do is touch inline markup. `**bold**` and `` `code` `` still come out
    escaped, because unpicking those needs a real markdown parser and getting it wrong is how
    an unbalanced marker restyles the rest of the message.

    One case it makes worse, which is worth knowing rather than hiding: nothing here parses
    markdown, so a line inside a fenced code block that begins with a dash is given a bullet it
    did not ask for. It is inside escaped backticks and reads as literal text either way.
    """
    text = _LINE_ENDINGS.sub("\n", text)
    text = _HTML_COMMENT.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTED.sub("", text)
    text = _BULLET.sub(_BULLET_MARK, text)
    return _BLANK_RUN.sub("\n\n", text).strip()


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
