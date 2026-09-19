"""What a message says, before anything decides how it looks. Issues #116 and #113.

A panel is an ordered list of blocks of text, plus three optional things that are structure rather
than words: an accent colour, a picture, and a link. Every renderer in `formatting` builds one of
these, and `layout` is the only module that turns one into something Discord understands.

The split is the point. The renderers stay pure and importable without discord.py, which is what
they were when they returned strings and is worth keeping: they are where the wording lives, they
are the most-tested code in the project, and none of that should depend on a UI library.

**The accent is optional, and `None` is not "no colour chosen".** It means this panel is not a card
at all and goes out as ordinary text. Four things in this project must stay that way: the two
relocation signposts, the note about commits that were not announced, and the line saying a
transcript could not be published. Each is deliberately quieter than the heading beside it, and a
coloured card there shouts over the thing it is there to point at.

**One block is one `TextDisplay`, never one per line.** That is what keeps the component count in
single figures against Discord's ceiling of forty, and it is why the ceiling is proved by a test
rather than guarded at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum

from shannon.discord_bot.safe_text import fit

# Discord allows four thousand display characters across a view, against the two thousand a plain
# message gets. The budget sits under that ceiling because `content_length` counts text displays
# and nothing else, and a button's label may or may not count against Discord's own accounting.
PANEL_BUDGET = 3900


class Accent(IntEnum):
    """The colour of the bar down the left of a card.

    GitHub's own state colours, in Primer's dark-mode values, because Discord draws that bar on a
    dark background for most readers and Primer's light greens and purples go muddy there.

    The names say what the colour MEANS rather than what it is, so a renderer picks a meaning and
    the table decides the shade. Several meanings share a value on purpose: a passing build and an
    open pull request are the same green because they are telling a reader the same thing.
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

    Kinds rather than free-form blocks because the shape is fixed: a heading and a subheading are
    grouped together when there is a picture to sit beside them, a rule goes above the body, and
    the footnote is the first thing dropped when a panel will not fit.
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
    """A picture to show under the blocks. Issue #126.

    The URL is somebody else's and the alt text is somebody else's words. Both are checked and made
    safe before they reach here, exactly as a block's text is: this module states what a message
    shows and never decides what is safe to show.

    `alt` is optional because an image written without any has none, and a word this project
    invented would tell somebody who cannot see the picture nothing.
    """

    url: str
    alt: str | None = None


@dataclass(frozen=True, slots=True)
class Panel:
    """A message, as the thing that wrote it sees it."""

    blocks: tuple[Block, ...] = ()
    accent: Accent | None = None
    # Only honoured beside a heading, because Discord hangs a thumbnail off a section and a
    # section is what a heading and its subheading become.
    thumbnail_url: str | None = None
    link: PanelLink | None = None
    # Pictures under the blocks, for the description they were lifted out of. NOT
    # counted by `length()`: Discord measures a view by its text displays and a gallery
    # is not one, so these cost nothing at all against the budget and exactly one
    # component against the ceiling of forty, however many pictures are in them.
    images: tuple[PanelImage, ...] = ()

    @classmethod
    def of_text(cls, text: str) -> Panel:
        """One block and nothing around it, which is what every plain message is.

        This is what lets a whole refactor land without changing a single byte on the wire: a
        panel built this way is plain, and a plain panel is sent as the string it came from.
        """
        return cls(blocks=(Block(BlockKind.BODY, text),) if text else ())

    @property
    def text(self) -> str:
        """Every block, in order, joined by a newline.

        A READING ORDER rather than a wire format, and the difference matters to anybody writing a
        test against it. What Discord renders puts a rule between some of these blocks and a
        coloured bar around all of them, so an assertion spanning a block boundary is asserting on
        something nothing produces.

        It is a projection and not a second renderer, which is the law `layout` is written to
        keep: the text displays in the view it builds are exactly these strings, in this order.
        Grouping, rules, the bar, the picture, the gallery and the button add structure and
        never words.
        """
        return "\n".join(block.text for block in self.blocks)

    @property
    def is_plain(self) -> bool:
        """Whether this is a message rather than a card.

        A plain panel has nothing structural to say, so it is sent as text and costs no components
        at all. Note this is not the same question as "does it have one block".

        The pictures are in here rather than left out as a detail. A card whose only structure is a
        gallery would otherwise be sent as the text it came from, and the pictures would simply not
        appear.
        """
        return (
            self.accent is None
            and self.thumbnail_url is None
            and self.link is None
            and not self.images
        )

    def length(self) -> int:
        """What this costs against the view budget, counted the way Discord counts it.

        The sum of the block texts, without the newlines `text` joins them with: those are this
        module's, not Discord's, and each block is its own component on the wire.

        The gallery is not in here and that is not an omission. `content_length` on the other side
        counts text displays and nothing else, so a gallery costs no part of the four thousand
        however many pictures it holds.
        """
        return sum(len(block.text) for block in self.blocks)

    def trimmed(self) -> Panel:
        """Cut to the budget by dropping whole blocks from the end, then trimming the survivor.

        The block-level twin of `fit`, and the same argument: dropping whole units leaves what
        remains rendering properly, where a cut at an arbitrary character can land inside a pair
        of asterisks and restyle everything after it.

        Dropping from the end is what makes the ordering of a panel a decision about what matters.
        The footnote goes first and the description after it, so the fields a reader scans for are
        the last thing to give way. That is the rule the block already had, said structurally
        rather than by arithmetic about lengths.
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
