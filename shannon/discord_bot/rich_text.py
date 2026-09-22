"""Keeping the formatting a description was written with.

`safe_text` strips GitHub markup; this keeps most of it, for the description block alone. Every
`](` in what comes out is one this module wrote, and points at a host GitHub serves.
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

# Discord's gallery takes ten. Four sits under a card without pushing the fields off a phone.
IMAGES_SHOWN = 4

# Discord's own ceiling on what a gallery item may be described as.
ALT_LIMIT = 256


# GitHub's web form submits CRLF, and every rule below is anchored to a line. Folded first so a
# blank line also costs one character against the preview limit rather than two.
_LINE_ENDINGS = re.compile(r"\r\n?")

# Invisible on GitHub and not here: a pull request template is mostly these, so without this the
# preview of a templated repository is the instructions to the author. Non-greedy, so two comments
# do not merge and swallow the text between them; an unterminated `<!--` is left as written.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# The space after the hashes is required. Without it the rule also strips a line-leading issue
# reference, so a description beginning `#3 is fixed by this` came out as `3 is fixed by this`.
_HEADING = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)

# GitHub reads `+` as a bullet and Discord does not, so a list written with one arrives as a
# paragraph of plus signs. `-` and `*` are Discord's own list syntax already, and are left alone.
_PLUS_BULLET = re.compile(r"^([ \t]*)\+([ \t]+)", re.MULTILINE)

# `-#` at the start of a line is Discord's subtext, small and grey; GitHub has never heard of it.
# Every footnote this bot writes uses it, so a description able to produce one could put words in
# the bot's mouth. Broken rather than stripped: on GitHub those are two characters somebody meant.
_SUBTEXT = re.compile(r"^([ \t]*-)(#)", re.MULTILINE)

_BLANK_RUN = re.compile(r"\n{3,}")

# Discord renders markdown images nowhere.
_IMAGE = re.compile(r"!\[([^\[\]\n]*)\]\(([^()<>\s]*)\)")

# The same shape without the bang, where discord.py's own `[.+](.+)` is greedy and runs to the
# last parenthesis on the line. Neither class crosses a bracket, parenthesis, space or newline, so
# a match is one link; `<` and `>` are out of the URL, so no link here can carry a mention.
_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()<>\s]*)\)")

# Discord opens a code block on three backticks wherever they appear, including mid-line.
_FENCE = re.compile(r"```")

# An image pasted into an issue is uploaded under `user-images.githubusercontent.com` or
# `github.com/user-attachments/`, and a file read out of a repository comes from
# `raw.githubusercontent.com`. Not `github.io`: anybody can take a subdomain and publish there.
_GITHUB_HOSTS = (GITHUB_HOST, "githubusercontent.com")


@dataclass(frozen=True, slots=True)
class Described:
    text: str
    images: tuple[PanelImage, ...] = ()


def as_rich_text(body: str) -> Described:
    """A description with the formatting it was written with, and its pictures lifted out.

    Images come out before the cut, so a report whose screenshots all sit past the preview limit
    still shows them. A cut can land inside a link or a marker, which the two steps after it undo.
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
    """Mentions dead, and safe to apply twice: neither rule matches its own output."""
    return defuse_mentions(discord.utils.escape_mentions(text))


def _inert(text: str) -> str:
    # A masked link is the one piece of markdown where what a reader sees and where they are taken
    # are different strings.
    return LINK_JOIN.sub("]" + ZERO_WIDTH_SPACE + "(", _defused(text))


def _linked(text: str) -> str:
    """Every link rewritten, and everything around them made inert.

    In fragments, not over the assembled string: sweeping the finished string would break the links
    this wrote, and sweeping first would let somebody type the thing the rewrite produces.
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

    A link off GitHub keeps its words and gains its host: `click here (evil.example)`. An empty
    label falls back to the host: Discord renders a masked link with no words as its own markup.
    """
    host = _host_of(url)
    if host is None:
        return _inert(f"[{label}]({url})")
    if _is_github(host):
        return f"[{label or _named(host)}]({url})"
    return f"{label} ({_named(host)})" if label else _named(host)


def _host_of(url: str) -> str | None:
    """`https` and nothing else, the rule `mapping._avatar` and `formatting._opens_github` share.

    An address that is merely odd rather than usable costs the item its whole block. Not
    `github.urls`, whose entry points all demand an owner and a repository and raise without them,
    which a release page, an avatar and a raw file have not got.
    """
    if not url.startswith("https://"):
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        # An unbalanced square bracket reads as a malformed IPv6 host, and `urlparse` raises rather
        # than answering.
        return None


def _is_github(host: str) -> bool:
    """The whole host or a dot and the whole host, never a bare suffix.

    `github.com.evil.example` ends with `github.com` under a plain `endswith` and is not GitHub.
    """
    return any(host == known or host.endswith(f".{known}") for known in _GITHUB_HOSTS)


def _named(host: str) -> str:
    """A host as something a reader can trust their eyes about.

    One spelled with a Cyrillic small letter i renders as `github.com` and is not, and `xn--` in
    front of it is visibly not the thing it imitates. The idna codec waves any ASCII label of
    sixty-three characters or fewer through, and a host it refuses is shown as written.
    """
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _shown(body: str) -> tuple[str, tuple[PanelImage, ...]]:
    """The body with its image markup lifted out, the alt text left where the markup was.

    Discord fetches a gallery's media before it will accept the message, so an address in an issue
    body that does not resolve costs the item its whole block. Only what GitHub serves becomes a
    picture; anything else keeps its alt text in the prose and is never fetched.
    """
    lifted: dict[str, PanelImage] = {}
    for image in _IMAGE.finditer(body):
        url = image.group(2)
        host = _host_of(url)
        if len(lifted) < IMAGES_SHOWN and host is not None and _is_github(host):
            lifted.setdefault(url, PanelImage(url=url, alt=_alt(image.group(1))))
    return _IMAGE.sub(lambda image: image.group(1), body), tuple(lifted.values())


def _alt(alt: str) -> str | None:
    """None rather than an invented word: "Image" tells a reader who cannot see it nothing."""
    said = _defused(alt).strip()[:ALT_LIMIT]
    return said or None


def _balanced(text: str) -> str:
    """Close a marker the writer left open, or the cut took the other half of.

    Bold runs past a newline and a fence swallows every line after it; the rest render as themselves
    unclosed. Not `github.safe_text.balanced`, which counts `~~~` as a fence as GitHub does: closing
    a fence Discord never opened would turn the rest of a description into code.
    """
    if text.count("**") % 2:
        text += "**"
    if len(_FENCE.findall(text)) % 2:
        text += "\n```"
    return text
