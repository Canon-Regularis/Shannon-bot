"""Making Discord-authored text safe to put in a GitHub comment, and short enough to post.

Discord's markdown and GitHub's are different languages, so none of the rules in the mirror
module `shannon/discord_bot/safe_text.py` carry over.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from shannon.domain.text import ZERO_WIDTH_SPACE, lines_within

# GitHub refuses a comment body over this with a 422.
GITHUB_BODY_LIMIT = 65536

# What one captured message may contribute. Discord's own ceiling on a message is 4000
# characters for a boosted account, so this cuts only what Discord was willing to carry.
LINE_LIMIT = 4000

TRUNCATED = "\n[...]"

# `@login` and `@org/team` both notify on GitHub. GitHub reads a mention only where the `@` is
# not preceded by a word character, so the lookbehind leaves `someone@example.com` as written
# and still catches a raw `<@123>` that never went through `clean_content`.
_MENTION = re.compile(r"(?<![\w/])@(?=[A-Za-z0-9])")

# `#123` is a cross-reference: it links the two items and puts a line in the other one's
# timeline. Digits required, so `# Heading` and a bare `#` are left alone.
_REFERENCE = re.compile(r"(?<![\w&])#(?=\d)")

# The other spelling of the same thing, which GitHub resolves just as happily.
_SHORTHAND = re.compile(r"\bGH-(?=\d)", re.IGNORECASE)

# An unterminated HTML comment hides the rest of the body on GitHub, so one message containing
# `<!--` would swallow everyone else's.
_COMMENT_OPEN = re.compile(r"<!(?=--)")

# GitHub reads a fence only at the start of a line. A balanced pair is left to render; an odd
# one has to be closed, because left open it swallows every line after it.
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)

_LINE_ENDINGS = re.compile(r"\r\n?")


def defuse(text: str) -> str:
    """Stop a captured message reaching out of the comment it is quoted in.

    A full `https://github.com/owner/repo/issues/40` URL is deliberately left alone, though
    GitHub cross-references it too: breaking shared links would cost more than the reference.
    """
    text = _MENTION.sub("@" + ZERO_WIDTH_SPACE, text)
    text = _REFERENCE.sub("#" + ZERO_WIDTH_SPACE, text)
    text = _SHORTHAND.sub("GH-" + ZERO_WIDTH_SPACE, text)
    return _COMMENT_OPEN.sub("<" + ZERO_WIDTH_SPACE + "!", text)


# Every character GitHub reads as inline markup. Escaping all of them is only safe on a name,
# where there is no such thing as markup somebody meant.
_INLINE_MARKUP = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")


def as_inline_text(text: str) -> str:
    """A name, rendered so it displays as written wherever it is dropped.

    Names go inside `**...**` in a transcript, so a display name containing `**` would close
    that early and embolden everybody who spoke afterwards.
    """
    return _INLINE_MARKUP.sub("\\\\\\1", defuse(text))


def balanced(text: str) -> str:
    """Close a code fence the writer left open, so it cannot swallow what comes after it.

    Nothing here parses markdown, so a fence inside a fence counts as a fence. Closing one that
    did not need it only adds an empty code block.
    """
    if len(_FENCE.findall(text)) % 2 == 0:
        return text
    return text + "\n```"


def as_a_tag(name: str) -> str:
    """Somebody's name where they were tagged, reading as a tag and ringing nobody.

    Two hazards, and neither covers the other: a display name that happens to be a GitHub login
    would notify whoever holds it (#121), and a name full of markup would restyle everybody
    else's words.
    """
    return "@" + ZERO_WIDTH_SPACE + as_inline_text(name)


# The token `discord_bot.capture` leaves where somebody tagged a person, and the only thing in a
# captured message allowed to become a live mention: the id came off `message.mentions` rather
# than out of the text, so Discord had already decided it was a mention.
_TAGGED = re.compile(r"<@([0-9]{15,20})>")


def one_message(content: str, tagged: Mapping[int, str] | None = None) -> str:
    """One captured Discord message, made safe and cut to what it may contribute.

    Two orderings are load-bearing. The RAW text is cut first, because cutting neutralised text
    can land between an `@` and the zero-width space protecting it and put the mention back.
    And the text is neutralised in FRAGMENTS, so `defuse` never sees a mention the caller
    supplied and no fragment can be turned into one.
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

    Lines rather than characters: a cut landing inside a fence or a link takes everything after
    it with it. The flush aims well under this limit, so reaching it means a single message was
    already near it.
    """
    if len(body) <= GITHUB_BODY_LIMIT:
        return body

    budget = GITHUB_BODY_LIMIT - len(TRUNCATED)
    kept = lines_within(body, budget)

    # Not passed through `balanced`, unlike the line cut below: a prefix of one over-long line
    # can still leave a fence open.
    if not kept:
        return body[:budget] + TRUNCATED
    return balanced("\n".join(kept) + TRUNCATED)
