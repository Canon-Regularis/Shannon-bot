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
from collections.abc import Mapping

from shannon.domain.text import lines_within

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


def as_a_tag(name: str) -> str:
    """Somebody's name where they were tagged, reading as a tag and ringing nobody.

    Two hazards at once and neither covers the other. The zero-width space stops a
    display name that happens to be a GitHub login notifying whoever holds it, which is
    the live bug issue #121 closes coming the other way: `@TheirDisplayName` used to
    reach GitHub intact. The escaping stops a name full of markup restyling everybody
    else's words.
    """
    return "@" + _INVISIBLE + as_inline_text(name)


# The token `discord_bot.capture` leaves where somebody tagged a person, and the only
# thing in a captured message allowed to become a live mention. The id in it came off
# `message.mentions` rather than out of the text, so Discord had already decided it was
# a mention of somebody; all that is decided here is how to spell it.
_TAGGED = re.compile(r"<@([0-9]{15,20})>")


def one_message(content: str, tagged: Mapping[int, str] | None = None) -> str:
    """One captured Discord message, made safe and cut to what it may contribute.

    Two orderings, and both are load-bearing.

    The RAW text is cut first and neutralised afterwards, which is the rule `clipped` on the
    Discord side spells out: cutting the neutralised text can land between an `@` and the
    zero-width space protecting it, which puts the mention back. A cut landing inside a `<@123>`
    leaves a token that no longer matches, so it stays in a fragment and is neutralised with the
    text around it. That is the safe way for it to fail, because it rings nobody.

    And the text is neutralised in FRAGMENTS, never as one assembled string. What the caller
    supplies is dropped BETWEEN the pieces of what somebody typed, so no `defuse` ever sees a
    mention this bot built and no fragment can be turned into one. An `@octocat` somebody typed is
    inside a fragment and comes out broken; an `@octocat` this bot put there is in no fragment at
    all. It is the rule `discord_bot.formatting._note` states in the other direction: the mention
    is built outside the untrusted text, because handing assembled text to a swap lets what
    somebody typed be read as a name.

    An id the caller says nothing about is left where it is, so the fragment it sits in defuses it.
    The map is the authority and the text is only ever a pointer into it.
    """
    text = _LINE_ENDINGS.sub("\n", content).strip()
    if len(text) > LINE_LIMIT:
        text = text[:LINE_LIMIT].rstrip() + "[...]"

    spelled = tagged or {}
    pieces: list[str] = []
    typed_from = 0
    for token in _TAGGED.finditer(text):
        mention = spelled.get(int(token.group(1)))
        if mention is None:
            continue
        pieces.append(defuse(text[typed_from : token.start()]))
        pieces.append(mention)
        typed_from = token.end()
    pieces.append(defuse(text[typed_from:]))
    return balanced("".join(pieces))


def fit_body(body: str) -> str:
    """Trim a whole comment to what GitHub will accept, on a line boundary.

    Lines rather than characters, for the reason `fit` gives on the Discord side: a cut landing
    inside a fence or a link takes everything after it with it. The flush aims well under this, so
    reaching it at all means one message was already close to the limit by itself.
    """
    if len(body) <= GITHUB_BODY_LIMIT:
        return body

    budget = GITHUB_BODY_LIMIT - len(TRUNCATED)
    kept = lines_within(body, budget)

    # Not mended, unlike the line cut below. A prefix of one over-long line can still open a
    # fence, so the difference is recorded here rather than relied on.
    if not kept:
        return body[:budget] + TRUNCATED
    return balanced("\n".join(kept) + TRUNCATED)
