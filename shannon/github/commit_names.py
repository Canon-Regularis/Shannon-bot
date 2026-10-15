"""The commits a GitHub body names, found by their shape the way GitHub links them. Issue #186.

GitHub turns a commit's hash in a comment into a link to the commit, and the thread showing the
same comment showed seven characters of nothing. This finds what GitHub would link without asking
GitHub: a run of hex standing on its own, outside code and outside a link or an address, the way
`mentions` finds a name.

Shape alone, which is the trade the issue chose. GitHub links a hash once it has found the commit;
this cannot look, so a hex word that is no commit links to GitHub's 404, and a hash written all in
digits or all in letters - which a number or a word would be just as often - is left as written.
The tests pin both rather than leave them to be discovered.

Not built on `mentions._runs`, which splits the text where code begins and reads each run on its
own. The character in front of a run is then invisible to a lookbehind, and here a backtick has to
refuse a hash, or `` `x`abc1234 `` would link a hash glued to a code span. Nor on its `_CODE`,
whose spans end at a line break and pair backticks one way where Discord pairs them another. One
scan instead, with everything to step over consumed whole before a hash is looked for.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from shannon.domain.models import RepositoryRef
from shannon.domain.text import LINK
from shannon.github.urls import OWNER, REPOSITORY, SHA

# What the preview puts where it cut a body short. A hash against it is a prefix the cut chose
# rather than the writer, so it is not read: the same mark `mentions` refuses a name against.
_CUT_SHORT = "…"

# A digit and a letter both. Seven digits are a number, and seven of a to f a word, far more often
# than either is a commit, so those are the hashes this leaves as written.
_HASH = rf"(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f]){SHA}"

# Standing alone. On the left, nothing a word, a path, a login, a reference, an escape, a link or a
# code span could be carrying on from: a hash against any of them is part of something else, and
# the dot and the hyphen are what keep one from cutting a team slug or a login short.
_FREE_BEFORE = r"(?<![\w/@#\\.\-\[\]!`])"

# On the right the same, less the dot, plus the cut mark. `@` keeps `abc1234@alice` from leaving a
# live name after the link. A dot or a colon that carries on into more - `abc1234.txt`,
# `abc1234..def5678`, `abc1234:README.md` - refuses too, and so does one the cut ran into, which
# could have been either; one that ends a sentence does not.
_FREE_AFTER = rf"(?![\w/@#\\\-\[\]`{_CUT_SHORT}]|[.:][\w.{_CUT_SHORT}])"

# Where GitHub reads nothing, consumed whole so no hash inside is looked at - by the rules Discord
# draws code with, because a link written into what Discord shows as code is shown as its markup.
#
# A fence first, so one block is one span. It holds something, as Discord's must, so six backticks
# open a fence rather than closing an empty one; and one the cut left open runs to the end, where
# `rich_text` closes it. Then a span of any number of backticks, closed by as many with no backtick
# after them, across lines as Discord's spans run: a closer taken from the front of a longer run
# would leave the rest of that run to `_balanced`, which counts it as a fence and closes it after
# the hash. A span may open on the backtick a fence closed on, as Discord's does.
_STEPPED_OVER = (
    r"```.+?(?:```|\Z)"
    r"|(?P<ticks>`+).*?[^`](?P=ticks)(?!`)"
    rf"|{LINK.pattern}"
    r"|https?://[^\s<>]*"
)

# `owner/repo@hash` is GitHub's spelling for a commit in another repository. The `@` may not follow
# a dot: GitHub reads `o/r.@abc1234` as a mention of `abc1234`, and so does `mentions`.
_NAMED = re.compile(
    rf"(?P<skipped>{_STEPPED_OVER})"
    rf"|{_FREE_BEFORE}(?:(?P<owner>{OWNER})/(?P<name>{REPOSITORY})(?<!\.)@)?"
    rf"(?P<sha>{_HASH}){_FREE_AFTER}",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class NamedCommit:
    """One hash a body writes, and where it is.

    `repository` is the one written in front of the hash, or None for a bare hash, which belongs to
    whichever repository the body does - a thing only the caller knows.
    """

    start: int
    end: int
    sha: str
    repository: RepositoryRef | None = None


def commits_named_in(text: str) -> Iterator[NamedCommit]:
    """Every hash `text` names, in the order it names them."""
    for match in _NAMED.finditer(text):
        sha = match.group("sha")
        if sha is None:
            continue
        owner, name = match.group("owner"), match.group("name")
        written = None if owner is None else RepositoryRef(owner=owner, name=name)
        yield NamedCommit(start=match.start(), end=match.end(), sha=sha, repository=written)
