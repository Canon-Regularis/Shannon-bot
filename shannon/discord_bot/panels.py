"""What a message says, before anything decides how it looks.

Renderers in `formatting` build panels and must stay importable without discord.py; `layout` is
the only module that turns one into Discord components. One block becomes one `TextDisplay`,
never one per line, which keeps a view under Discord's ceiling of forty components.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum

from shannon.discord_bot.safe_text import fit

# Discord allows four thousand display characters across a view, against the two thousand a plain
# message gets. The budget sits under that ceiling because a button's label may or may not count
# against Discord's own accounting.
PANEL_BUDGET = 3900


class Accent(IntEnum):
    """The colour of the bar down the left of a card.

    GitHub's state colours in Primer's dark-mode values, because Discord draws the bar on a dark
    background and Primer's light greens and purples go muddy there. Names say what the colour
    means, and meanings that tell a reader the same thing deliberately share a value.
    """

    OPEN = 0x3FB950
    PASSED = 0x3FB950
    LOW = 0x3FB950

    NEUTRAL = 0x8B949E
    DRAFT = 0x8B949E

    MERGED = 0xA371F7

    CLOSED = 0xF85149
    FAILED = 0xF85149
    HIGH = 0xF85149

    MEDIUM = 0xD29922

    SAID = 0x58A6FF


class BlockKind(StrEnum):
    """What a block is for, which is how `layout` decides what to put around it.

    A heading and its subheading are grouped together when there is a picture to sit beside them,
    and a rule goes above the body.
    """

    HEADING = "HEADING"
    SUBHEADING = "SUBHEADING"
    FIELDS = "FIELDS"
    BODY = "BODY"
    FOOTNOTE = "FOOTNOTE"


@dataclass(frozen=True, slots=True)
class Block:
    """One run of text. Already safe, already cut, never empty."""

    kind: BlockKind
    text: str


@dataclass(frozen=True, slots=True)
class PanelLink:
    """A button that opens a URL.

    The label is this project's own words and never GitHub's, because a button label is one of the
    few places untrusted text could not be escaped into safety.
    """

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class PanelImage:
    """A picture to show under the blocks.

    The URL and the alt text are somebody else's, checked and made safe before they reach here.
    `alt` is `None` when the source image had none, rather than a word this project invented.
    """

    url: str
    alt: str | None = None


@dataclass(frozen=True, slots=True)
class Panel:
    """A message, as the thing that wrote it sees it."""

    blocks: tuple[Block, ...] = ()
    # `None` means not a card at all: the panel goes out as ordinary text. The relocation
    # signposts, the unannounced-commits note and the unpublished-transcript line rely on that.
    accent: Accent | None = None
    # Only honoured beside a heading, because Discord hangs a thumbnail off a section and a
    # section is what a heading and its subheading become.
    thumbnail_url: str | None = None
    link: PanelLink | None = None
    # Not counted by `length()`: Discord measures a view by its text displays, and a gallery is
    # one component against the ceiling of forty and no characters against the budget, however
    # many pictures it holds.
    images: tuple[PanelImage, ...] = ()

    @classmethod
    def of_text(cls, text: str) -> Panel:
        """One block and nothing around it, which is what every plain message is."""
        return cls(blocks=(Block(BlockKind.BODY, text),) if text else ())

    @property
    def text(self) -> str:
        """Every block, in order, joined by a newline.

        A reading order, not the wire format: `layout` sends each block as its own text display,
        with rules and a coloured bar between them, so a test assertion spanning a block boundary
        asserts on something nothing produces. Those text displays are exactly these strings.
        """
        return "\n".join(block.text for block in self.blocks)

    @property
    def is_plain(self) -> bool:
        """Whether this is a message rather than a card.

        A plain panel is sent as text and costs no components. Images count as structure: a card
        whose only structure is a gallery would otherwise go out as the text it came from, and the
        pictures would not appear.
        """
        return (
            self.accent is None
            and self.thumbnail_url is None
            and self.link is None
            and not self.images
        )

    def length(self) -> int:
        """What this costs against the view budget, counted the way Discord counts it.

        Without the newlines `text` joins the blocks with: those are this module's, not
        Discord's, and each block goes on the wire as its own text display.
        """
        return sum(len(block.text) for block in self.blocks)

    def trimmed(self) -> Panel:
        """Cut to the budget by dropping whole blocks from the end, then trimming the survivor.

        Whole blocks rather than characters, as in `fit`: a cut at an arbitrary character can land
        inside a pair of asterisks and restyle everything after it. Panels are ordered so the
        footnote goes first and the description next, leaving the fields a reader scans for last.
        """
        if self.length() <= PANEL_BUDGET:
            return self

        kept = list(self.blocks)
        while len(kept) > 1 and sum(len(block.text) for block in kept) > PANEL_BUDGET:
            kept.pop()

        last = kept[-1]
        room = PANEL_BUDGET - sum(len(block.text) for block in kept[:-1])
        kept[-1] = replace(last, text=fit(last.text, limit=room))
        return replace(self, blocks=tuple(kept))
