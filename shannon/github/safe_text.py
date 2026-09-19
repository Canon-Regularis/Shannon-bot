"""Making Discord-authored text safe to put in a GitHub comment, and short enough to post.

The mirror image of `shannon/discord_bot/safe_text.py`, which does this in the direction every
other feature runs. Issue #103 is the first thing here that writes what a person typed into a
place that renders it, so the same hazards exist the other way round and none of that module's
rules apply: Discord's markdown and GitHub's are different languages with different weapons.

Everything here takes a string and returns a string. It knows what GitHub's markdown does with
hostile input, and nothing about threads or conversations.
"""

from __future__ import annotations

import re

# GitHub refuses a comment body over this with a 422. The transcript budget below is what the
# flush actually aims at, so this is the backstop for a single message that is already enormous.
GITHUB_BODY_LIMIT = 65536

# What one captured message may contribute. Discord's own ceiling on a message is 4000 characters
# for a boosted account, so this cuts only what Discord itself was willing to carry, and it stops
# one person's wall of text being the whole comment.
LINE_LIMIT = 4000

TRUNCATED = "\n[...]"

# A zero-width space, the same trick `defuse_mentions` uses on the way in. It costs a reader
# nothing and it is invisible in the rendered comment.
_INVISIBLE = "​"

# `@login` and `@org/team` both notify on GitHub, and a transcript is full of names. The lookbehind
# is the whole of what makes this safe to apply everywhere: GitHub reads a mention only where the
# `@` is not preceded by a word character, so `someone@example.com` is left exactly as written.
#
# This also catches a raw `<@123>` that reached here without going through `clean_content`, since
# a digit is a word character and the `@` before it is preceded by `<`.
_MENTION = re.compile(r"(?<![\w/])@(?=[A-Za-z0-9])")

# `#123` is a cross-reference, and a transcript saying "fixed by #40" would link the two items and
# put a line in the other one's timeline. Digits required, so `# Heading` and a bare `#` are left
# alone: a heading is cosmetic and this rule is not about cosmetics.
_REFERENCE = re.compile(r"(?<![\w&])#(?=\d)")

# The other spelling of the same thing, which GitHub resolves just as happily.
_SHORTHAND = re.compile(r"\bGH-(?=\d)", re.IGNORECASE)

# Invisible on GitHub, which is the problem. An unterminated one comments out the rest of the
# body, so a single message containing `<!--` would swallow everyone else's. Broken rather than
# stripped, because this is somebody's words and hiding part of them is worse than showing the
# marker they typed.
_COMMENT_OPEN = re.compile(r"<!(?=--)")

# Line-leading only, which is what GitHub reads as a fence. Counted rather than neutralised,
# because pasting code into a thread is an ordinary thing to do and a balanced fence renders the
# way whoever pasted it meant. It is the odd one that has to be closed: left open it swallows
# every line after it, including the attribution of everybody who spoke next.
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)

_LINE_ENDINGS = re.compile(r"\r\n?")


def defuse(text: str) -> str:
    """Stop a captured message reaching out of the comment it is quoted in.

    Four rules, each closing something that acts on a person or an item who was never part of the
    conversation. A mention subscribes an account to the thread, a reference writes a line into
    another item's timeline, and an HTML comment hides whatever follows it.

    Deliberately not touched: a full `https://github.com/owner/repo/issues/40` URL, which GitHub
    also cross-references. Somebody sharing a link is the point of this feature, and breaking the
    link to avoid the reference would cost more than the reference does. The README says so.
    """
    text = _MENTION.sub("@" + _INVISIBLE, text)
    text = _REFERENCE.sub("#" + _INVISIBLE, text)
    text = _SHORTHAND.sub("GH-" + _INVISIBLE, text)
    return _COMMENT_OPEN.sub("<" + _INVISIBLE + "!", text)


# Every character GitHub reads as inline markup. Escaped one at a time, which is safe here in a
# way it would not be for a message body: this is only ever used on a name, where there is no
# such thing as markup somebody meant.
_INLINE_MARKUP = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")


def as_inline_text(text: str) -> str:
    """A name, rendered so it displays as written wherever it is dropped.

    Names go inside `**...**` in a transcript, and a display name containing `**` would close that
    early and embolden everybody who spoke afterwards. `defuse` alone does not help: it stops a
    name notifying an account, which is a different hazard from a name restyling the comment.
    """
    return _INLINE_MARKUP.sub("\\\\\\1", defuse(text))


def balanced(text: str) -> str:
    """Close a code fence the writer left open, so it cannot swallow what comes after it.

    An odd number of fences is the only case that needs anything. Nothing here parses markdown, so
    a fence inside a fence is counted as a fence, which is the conservative way round: closing one
    that did not need it adds an empty code block, and leaving one open loses the rest of the
    comment.
    """
    if len(_FENCE.findall(text)) % 2 == 0:
        return text
    return text + "\n```"


def one_message(content: str) -> str:
    """One captured Discord message, made safe and cut to what it may contribute.

    The order is the whole of it, and it is the one `clipped` on the Discord side spells out: the
    RAW text is cut first and neutralised afterwards. Cutting the neutralised text instead can
    land between an `@` and the zero-width space protecting it, which puts the mention back.
    """
    text = _LINE_ENDINGS.sub("\n", content).strip()
    if len(text) > LINE_LIMIT:
        text = text[:LINE_LIMIT].rstrip() + "[...]"
    return balanced(defuse(text))


def fit_body(body: str) -> str:
    """Trim a whole comment to what GitHub will accept, on a line boundary.

    Lines rather than characters, for the reason `fit` gives on the Discord side: a cut landing
    inside a fence or a link takes everything after it with it. The flush aims well under this, so
    reaching it at all means one message was already close to the limit by itself.
    """
    if len(body) <= GITHUB_BODY_LIMIT:
        return body

    budget = GITHUB_BODY_LIMIT - len(TRUNCATED)
    kept: list[str] = []
    used = 0
    # No ordinary exit, for the reason its Discord counterpart says: this runs only above the
    # limit, the per-line costs sum to the length of the body, and the budget is smaller.
    for line in body.split("\n"):  # pragma: no branch
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost

    if not kept:
        return body[:budget] + TRUNCATED
    return balanced("\n".join(kept) + TRUNCATED)
