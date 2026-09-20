"""Keeping the formatting a description was written with. Issues #125 and #126.

The one module in this project that lets GitHub markup through. Everything in `safe_text` does the
opposite and is right to: a title, a comment body and a commit message are single lines dropped
between eleven matched `**Label:** value` pairs, and a marker surviving any of them restyles the
card. This is for the description block and nothing else, which is a compartment of its own with a
rule above it and its own label.

Its own file rather than a function beside `as_plain_text`, because one file holding both "kill
every marker" and "keep most of them" is how somebody later calls the wrong one on a comment body.
The names are meant to be read together: `as_plain_text` and `as_rich_text`.

**The rule this rests on, and it is the whole of the safety argument:**

    Every `](` in what comes out is one this module wrote, and every one it wrote points at a host
    GitHub serves.

Held structurally rather than by care. The text is neutralised in FRAGMENTS and what this module
writes is dropped BETWEEN them, so no sweep ever sees a link this module built and no amount of
typing produces one. `github/safe_text.one_message` and `formatting._note` state the same rule in
their own directions.

Deliberately not reached: `discord.utils.escape_markdown`. Its `[.+](.+)` alternative is greedy, so
on a line carrying a link it runs to the last parenthesis and ships everything between unescaped,
and `safe_text.LINK_JOIN` exists to stop it matching at all. Nothing here calls it, so there is
nothing to defeat, and the title path keeps both the rule and the tests that prove it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import discord

from shannon.discord_bot.panels import PanelImage
from shannon.discord_bot.safe_text import (
    DESCRIPTION_PREVIEW_LIMIT,
    LINK_JOIN,
    cut,
    defuse_mentions,
)
from shannon.domain.text import ZERO_WIDTH_SPACE
from shannon.github.urls import GITHUB_HOST

# Up to four. Discord's gallery takes ten, and ten screenshots is not a description, it is the
# whole channel. Four sits under a card without pushing the fields off a phone, and every one is a
# fetch Discord performs before it will accept the message at all.
IMAGES_SHOWN = 4

# Discord's own ceiling on what a gallery item may be described as.
ALT_LIMIT = 256


# GitHub's web form submits CRLF, and every rule below is anchored to a line. Folded first so the
# rest of them see one kind of line ending, and so a blank line costs one character against the
# preview limit rather than two.
_LINE_ENDINGS = re.compile(r"\r\n?")

# Invisible on GitHub and very much not here. A pull request template opens with one of these and
# carries more between its sections, so without this the preview of a templated repository is the
# instructions to the author rather than anything the author wrote.
#
# Non-greedy, so two comments do not merge into one match and swallow the description between
# them. An unterminated `<!--` matches nothing and is left as written, which is the safe way
# round: the alternative eats the rest of the body.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# A space after the hashes is required, and that is the whole of what makes this safe. Without it
# the rule also strips a line-leading issue reference, so a description beginning `#3 is fixed by
# this` came out as `3 is fixed by this` with the reference gone. GitHub wants the space for a
# heading anyway, so demanding it is the more correct reading as well as the safer.
#
# Headings still go, now that everything around them stays. A card built out of labels has one
# voice already, and a description able to write a heading inside it has two.
_HEADING = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)

# GitHub reads `+` as a bullet and Discord does not, so a list written with one arrives as a
# paragraph of plus signs. `-` and `*` are left exactly as written, which is the change issue #125
# asked for: both are already Discord's own list syntax.
_PLUS_BULLET = re.compile(r"^([ \t]*)\+([ \t]+)", re.MULTILINE)

# Discord's subtext, which GitHub has never heard of: `-#` at the start of a line renders small and
# grey, and that is this bot's own voice. Every footnote in the project is written in it, so a
# description able to produce one could put words in the bot's mouth inside the bot's own card.
#
# It was dead before this change only by accident. `escape_markdown` backslashes a line-leading
# dash, and nothing escapes a description any more. Broken rather than stripped, because on GitHub
# those are two characters somebody typed and meant.
_SUBTEXT = re.compile(r"^([ \t]*-)(#)", re.MULTILINE)

_BLANK_RUN = re.compile(r"\n{3,}")

# What Discord renders nowhere, and what issue #126 lifts into a real gallery.
_IMAGE = re.compile(r"!\[([^\[\]\n]*)\]\(([^()<>\s]*)\)")

# The same shape without the bang. The character classes are the whole of what makes this safe to
# run where discord.py's own `[.+](.+)` is not: neither can cross a bracket, a parenthesis, a space
# or a newline, so a match is one link and never a line.
#
# `<` and `>` are excluded from the URL as well, so no link this module emits can carry a `<@123>`
# through a part of the text nothing else sweeps.
_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()<>\s]*)\)")

# Discord opens a code block on three backticks wherever they appear, including mid-line. That is
# where this parts company with its GitHub-side twin, which is line-anchored.
_FENCE = re.compile(r"```")

# `github.com` itself, and the host GitHub serves user content from. Naming only the first would
# refuse every screenshot in the product: an image pasted into an issue is uploaded under
# `user-images.githubusercontent.com` or `github.com/user-attachments/`, and a file read out of a
# repository comes from `raw.githubusercontent.com`.
#
# Deliberately NOT `github.io`. That is a page anybody can publish on a domain anybody can take a
# subdomain of, so a masked link there hides a destination this project cannot vouch for, which is
# the whole of what the rule is for.
_GITHUB_HOSTS = (GITHUB_HOST, "githubusercontent.com")


@dataclass(frozen=True, slots=True)
class Described:
    """A description, and the pictures that came out of it."""

    text: str
    images: tuple[PanelImage, ...] = ()


def as_rich_text(body: str) -> Described:
    """A description with the formatting it was written with, and its pictures lifted out.

    The order is the design. Images come out BEFORE the cut, and that is the feature rather than an
    optimisation: a bug report whose screenshots all sit past character seven hundred is an
    ordinary bug report, and cutting first would show none of them, which is the issue. It also
    stops a hundred characters of opaque URL being spent from a budget meant to hold words.

    The cut itself stays on the raw text, which is `cut`'s rule and reason. What is new is that a
    cut can land in the middle of a link or a marker, and the two steps after it are what make that
    safe rather than any attempt to cut more carefully: a severed `](` is broken apart, and an odd
    `**` is closed. Balancing is last because it is the only step that appends.
    """
    text = _LINE_ENDINGS.sub("\n", body or "")
    text = _HTML_COMMENT.sub("", text)
    text, images = _shown(text)
    text = _HEADING.sub("", text)
    text = _PLUS_BULLET.sub(r"\1-\2", text)
    text = _SUBTEXT.sub("\\1" + ZERO_WIDTH_SPACE + "\\2", text)
    text = _BLANK_RUN.sub("\n\n", text)
    text = cut(text, limit=DESCRIPTION_PREVIEW_LIMIT)
    return Described(text=_balanced(_linked(text)), images=images)


def _defused(text: str) -> str:
    """Every mention dead, and nothing else touched.

    Safe to apply twice: both rules leave a zero-width space where the pattern was, and neither
    pattern matches its own output. The client honours user and role mentions, so a live one here
    would ring somebody.
    """
    return defuse_mentions(discord.utils.escape_mentions(text))


def _inert(text: str) -> str:
    """Text nobody vetted: mentions dead, and no bracket left touching a parenthesis."""
    # Every `](` the link rule did not write itself, broken the way `safe_text` breaks them:
    # a masked link is the one piece of markdown where what a reader sees and where they are
    # taken are different strings.
    return LINK_JOIN.sub("]" + ZERO_WIDTH_SPACE + "(", _defused(text))


def _linked(text: str) -> str:
    """Every link rewritten, and everything around them made inert.

    In FRAGMENTS, never over the assembled string. What this module writes is dropped BETWEEN the
    pieces of what somebody typed, so no sweep sees a link this module built and no amount of
    typing produces one. Sweeping the finished string would break the links this wrote; sweeping
    first and rewriting after would let somebody type the thing the rewrite produces.
    """
    pieces: list[str] = []
    typed_from = 0
    for link in _LINK.finditer(text):
        pieces.append(_inert(text[typed_from : link.start()]))
        pieces.append(_destination(_defused(link.group(1)), link.group(2)))
        typed_from = link.end()
    pieces.append(_inert(text[typed_from:]))
    return "".join(pieces)


def _destination(label: str, url: str) -> str:
    """One link, rendered so a reader knows where it goes before they click.

    Three answers and no fourth. A link to GitHub is a masked link, because that is where every
    link in a mirrored description is meant to point and standing a word in front of a URL is what
    markdown is for. A link anywhere else keeps its words and gains its host, so
    `[click here](https://evil.example/x)` reads `click here (evil.example)`: nothing is blocked
    and nothing is hidden, and a reader can see that "the docs" are not on the docs' host. Anything
    that is not an `https` URL at all is not a link here, and the characters somebody typed are
    shown as typed with the bracket broken off the parenthesis, so Discord builds nothing out of
    them and nothing is silently removed either.

    An empty label is answered rather than fallen into: Discord renders a masked link with no words
    as its own markup, so the host stands in for the words nobody wrote.
    """
    host = _host_of(url)
    if host is None:
        return _inert(f"[{label}]({url})")
    if _is_github(host):
        return f"[{label or _named(host)}]({url})"
    return f"{label} ({_named(host)})" if label else _named(host)


def _host_of(url: str) -> str | None:
    """The host of an `https` URL, or None for anything that is not one.

    `https` and nothing else, which is the rule `mapping._avatar` and `formatting._opens_github`
    already apply, for the reason `_avatar` records: a value that is merely odd rather than usable
    does not cost a card its picture, it costs the item its block.

    Not `github.urls`, though the host constant comes from there. Every entry point in that module
    demands an owner and a repository and raises without them, and a release page, an avatar and a
    raw file have neither.
    """
    if not url.startswith("https://"):
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        # An unbalanced square bracket reads as a malformed IPv6 host and `urlparse` raises rather
        # than answering. `github/urls.py` hit exactly this and says so.
        return None


def _is_github(host: str) -> bool:
    """Whether GitHub serves this host.

    The whole host or a dot and the whole host, never a bare suffix: `github.com.evil.example` ends
    with `github.com` under a plain `endswith` and is not GitHub at all.
    """
    return any(host == known or host.endswith(f".{known}") for known in _GITHUB_HOSTS)


def _named(host: str) -> str:
    """A host as something a reader can trust their eyes about.

    Punycode, because the point of naming a host is that somebody can read it and tell. A host
    spelled with a Cyrillic small letter i renders as `github.com` and is not, and `xn--` in front
    of it is visibly not the thing it is imitating. An ASCII host encodes to itself, including one
    nobody could register: the codec waves through any ASCII label of sixty-three characters or
    fewer rather than applying the rules that would refuse an underscore. A host it does refuse,
    such as one carrying a longer label than that, is shown as written: it is being read rather
    than followed, and showing it wrong is worse than showing it oddly.
    """
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _shown(body: str) -> tuple[str, tuple[PanelImage, ...]]:
    """The body with its image markup lifted out, and the pictures that came out of it.

    The alt text is left exactly where the markup was, so "here is the crash: ![the stack trace]"
    still reads as a sentence. An image written with no alt text leaves nothing behind, because
    what was there was a URL.

    Only what GitHub itself serves becomes a picture, by the rule links follow and for a harder
    reason than links have. Discord fetches a gallery's media before it will accept the message, so
    an address in an issue body is an address anybody can point this bot at, and one that does not
    resolve costs the item its whole block rather than costing the block a picture. Anything else
    keeps its alt text in the prose and is never fetched.

    Four at most and never the same URL twice, so a template repeating one badge cannot spend the
    gallery on it. The first alt text wins, because that is the one written where it was explained.
    """
    lifted: dict[str, PanelImage] = {}
    for image in _IMAGE.finditer(body):
        url = image.group(2)
        host = _host_of(url)
        if len(lifted) < IMAGES_SHOWN and host is not None and _is_github(host):
            lifted.setdefault(url, PanelImage(url=url, alt=_alt(image.group(1))))
    return _IMAGE.sub(lambda image: image.group(1), body), tuple(lifted.values())


def _alt(alt: str) -> str | None:
    """What somebody who cannot see a picture is told it is, or nothing when nobody said.

    GitHub's words, so the mentions in them are killed like everywhere else. None rather than a
    word this project invented: "Image" tells a reader nothing they cannot already see, and
    somebody who cannot see it nothing at all.
    """
    said = _defused(alt).strip()[:ALT_LIMIT]
    return said or None


def _balanced(text: str) -> str:
    """Close a marker the writer left open, or the cut above took the other half of.

    Two markers and not six. Bold is the one that runs past a newline, and a fence is the one that
    swallows every line after it. Italics, strikethrough, spoilers and a single backtick render as
    the characters they are when nothing closes them.

    Discord draws each text display on its own, so an open marker here should not reach the fields
    above it. "Should not" is precisely why this runs anyway: it is two lines, the whole card is
    built out of matched `**Label:** value` pairs, and that is not a layout to bet on a renderer
    behaving the way the documentation implies.

    NOT `github.safe_text.balanced`, which answers the same question in the other language. That
    one counts `~~~` as a fence because GitHub reads one, and appending three backticks for a fence
    Discord never opened would turn the rest of a description into a code block.
    """
    if text.count("**") % 2:
        text += "**"
    if len(_FENCE.findall(text)) % 2:
        text += "\n```"
    return text
