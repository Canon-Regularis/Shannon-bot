"""The names a GitHub body writes, and putting something else in their place.

Two jobs that have to agree, which is why they are one module. Reading the names out of a body is
what tells the database which rows to fetch; swapping them for mentions is what a reader sees. If
the two ever disagreed about what counts as a name, the disagreement would show up as a mention
silently not happening, because a name nothing fetched a row for renders exactly as a name nobody
has linked.

Nothing here talks to GitHub and nothing here writes Discord syntax. It knows what a GitHub name
looks like, and it hands each one it finds to a renderer the caller supplies.

One thing it does know about Discord, and it is worth saying rather than hiding. The text handed
to `rewrite` has already been through `safe_text.as_plain_text`, which puts a backslash in front of
an underscore and a zero-width space inside anything that already looked like a mention. The
pattern below accommodates both, because the pattern is the only place that can: escaping the text
first is what makes every other character in it safe, and the alternative is substituting before
the escaping and then trying to protect the results from it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from shannon.domain.text import ZERO_WIDTH_SPACE

# What the caller does with a name: the text to put in its place, or None to leave it as written.
Render = Callable[[str], str | None]

# How many distinct names in one body are worth answering, in either direction. Reading them
# out of a GitHub comment is where this started; since issue #121 it also caps how many
# accounts a published transcript may tag, because the sentence below is about what a body
# does to the people in it and does not care which way the body was built.
#
# Without a limit this is a broadcast weapon. The preview is capped at seven hundred characters,
# which holds about a hundred and thirty names, and a message trimmed to Discord's limit still
# carried eighty-two live pings when it was measured. Anyone who can comment on the repository
# could reach every linked member of the server, over and over, for nothing.
#
# Ten is past the point where a comment is asking people something and into announcing at them.
# Names beyond it are left as written, so the thread still shows every one of them and nothing is
# hidden; what is lost is the notification, silently, which is what an unlinked name already does.
MENTION_LIMIT = 10

# GitHub's own rule, which is narrower than "letters, digits and hyphens": a login may not begin
# or end with a hyphen and may not carry two in a row. Written out because the loose version
# accepts `mona--lisa` and `monalisa-`, names GitHub cannot issue.
LOGIN = r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}"

# A team slug is more forgiving: GitHub builds it from the display name, so it takes underscores
# and full stops that an account name never would, and it can be longer.
TEAM_SLUG = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98})"

# `\A` and `\Z` rather than `^` and `$`, which is not pedantry: `$` also matches before a
# trailing newline, so `^...$` called `alice\n` a login. That is what `/link` validates what
# somebody types with, and what the transcript checks before putting one in a URL.
_IS_LOGIN = re.compile(rf"\A{LOGIN}\Z")
_IS_TEAM_SLUG = re.compile(rf"\A{TEAM_SLUG}\Z")

# A slug, in either shape it can be in. `_` is the one character of a slug that
# `escape_markdown` touches, and it comes out as a backslash and an underscore, so both forms are
# alternatives here and are folded back together when the name is read.
#
# Both shapes rather than only the escaped one, although both callers hand this escaped text
# today. It costs a character, it makes the pattern answer the same way about a body whether or
# not it has been through the escaping, and the alternative was found by writing it: reading the
# raw body made `names_in` stop at `back` and ask about a team of that name while the swap went
# looking for `back_end`, which is the under-fetch that loses a mention in silence.
#
# The doubled backslash is load-bearing and invisible to read. `\_` also compiles, also matches,
# and stops the slug at the first underscore, so `@canon/back_end` would quietly ask about a team
# called `back`. `test_an_escaped_underscore_is_read_back_as_one` is there for that alone.
_SLUG_IN_TEXT = r"[A-Za-z0-9](?:(?:[A-Za-z0-9._-]|\\_)*[A-Za-z0-9])?"


# What the preview puts where it cut the body short. A name with this against it was cut in half,
# so what is left is a prefix of somebody's name and not a name: `@monalisa` shows as `@mona`,
# and where somebody called `mona` is also linked, rendering it would ping a person the comment
# never named. Refusing it is the only answer that pings nobody rather than the wrong somebody.
_CUT_SHORT = "…"

# What may not come immediately before the `@`.
#
# The zero-width space is what stops a mention that has already been taken apart being put back
# together, and it is worth being exact about which half it guards, because the two defused forms
# are defused in different places. `<@123>` and `<@&123>` come back with the space between the
# `<` and the `@`, which is here, so this is the only thing refusing them. `@everyone`, `@here`
# and a mention written as bare digits come back with the space after the `@` instead, so what
# refuses those is the name itself having to start with a letter or a digit.
#
# Both are proved by mutation rather than by reading: take this character out and the hand-written
# `<@123>` test fails while the `@everyone` one still passes.
#
# `<` and `/` are the second guard, and they defend against a different mistake. The header line
# of a note carries a mention this bot built itself, live and never defused, and a GitHub login
# may be all digits, so `<@7>` would otherwise be read as a name and rewritten into somebody
# else. The rewrite is only ever applied to a quoted body and never to an assembled message, and
# this is what makes that a belt as well as a rule. `/` keeps a name out of a pasted URL.
_BEFORE_A_NAME = f"(?<![A-Za-z0-9_@/<{ZERO_WIDTH_SPACE}-])"

# The team alternative is first on purpose. An organisation can share a name with somebody who
# has linked an account, and trying the person first would ping them in their team's place.
_MENTION = re.compile(
    _BEFORE_A_NAME
    + rf"@(?:(?P<org>{LOGIN})/(?P<team>{_SLUG_IN_TEXT})(?![A-Za-z0-9{_CUT_SHORT}])"
    # The team guard refuses a trailing letter as well as the cut marker, which the user branch
    # gets for free. Without it the slug simply backtracks to a shorter one and `@canon/back…`
    # matches a team called `bac`.
    + rf"|(?P<user>{LOGIN})(?![A-Za-z0-9/{_CUT_SHORT}-]))"
)


def is_login(name: str) -> bool:
    """Whether this is shaped like a GitHub account name."""
    return _IS_LOGIN.match(name) is not None


def is_team_slug(name: str) -> bool:
    """Whether this is shaped like a GitHub team slug."""
    return _IS_TEAM_SLUG.match(name) is not None


@dataclass(frozen=True, slots=True)
class Mentioned:
    """The names a body writes, lowercased and in the order they appear.

    Two tuples rather than one, because a slug that happens to match a login is not that person.
    They are looked up in different tables and Discord writes them with different syntax, and
    rendering one as the other puts somebody's name against a team they have nothing to do with.
    """

    people: tuple[str, ...]
    teams: tuple[str, ...]


def names_in(text: str) -> Mentioned:
    """Every name this body writes, up to the limit.

    Asked of the same text that will be shown, which matters more than it looks. The preview is
    cut before the escaping and the cut lands mid-word, so a body ending `@monalisa` can be shown
    as `@mona`. Read from the whole body instead, this would ask about `monalisa` and the swap
    below would be handed `mona`, and if somebody called `mona` were also linked the tail of one
    person's name would ping another.

    Deliberately more willing to ask than the swap is to answer. A name it asks about that is
    never rendered costs one wasted entry in a query; a name it fails to ask about is a mention
    that silently does not happen, because nothing tells a name with no row from a name nobody
    linked. `@everyone` is the clearest case: it is asked about here and can never be rendered,
    and excluding it would be this function knowing what the escaping does, which is the coupling
    worth avoiding.
    """
    people: list[str] = []
    teams: list[str] = []
    seen: set[tuple[bool, str]] = set()

    for match in _MENTION.finditer(text):
        is_team, name = _read(match)
        key = (is_team, name)
        if key in seen:
            continue
        if len(seen) >= MENTION_LIMIT:
            break
        seen.add(key)
        (teams if is_team else people).append(name)

    return Mentioned(people=tuple(people), teams=tuple(teams))


def rewrite(text: str, *, person: Render, team: Render) -> str:
    """Put each name the caller answers for in its place, and leave the rest as written.

    The budget is spent on distinct names rather than on occurrences, which is the same thing
    `names_in` counts, so the two always reach the same distance into a body. Counting
    occurrences here would let this look at names the lookup never asked about; counting only
    the names that resolved would let it look further still.

    Answered once and reused, so a name written three times reads the same all three times.
    """
    answered: dict[tuple[bool, str], str | None] = {}

    def swap(match: re.Match[str]) -> str:
        is_team, name = _read(match)
        key = (is_team, name)
        if key not in answered:
            if len(answered) >= MENTION_LIMIT:
                return match.group(0)
            answered[key] = team(name) if is_team else person(name)
        return answered[key] or match.group(0)

    return _MENTION.sub(swap, text)


def _read(match: re.Match[str]) -> tuple[bool, str]:
    """Which kind of name this is, and the name itself, lowercased.

    The organisation in front of a team is read only to tell the two kinds apart and is then
    dropped. `/link_team` stores a bare slug and the table is scoped to one server, so the slug
    is the whole of what identifies a team here.
    """
    slug = match.group("team")
    if slug is not None:
        return True, slug.replace("\\_", "_").lower()
    return False, match.group("user").lower()
