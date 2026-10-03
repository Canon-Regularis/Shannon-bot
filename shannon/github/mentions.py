"""The names a GitHub body writes, and putting something else in their place.

Reading and swapping live together because they must agree about what counts as a name. Text may
have been through `safe_text.as_plain_text`, which backslash-escapes an underscore and puts a
zero-width space inside anything that already looked like a mention, so the pattern below has to
accommodate both shapes - and since issue #166 a note's body may instead arrive with its markdown
intact, which is what `skipping_code` is for.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from shannon.domain.text import ZERO_WIDTH_SPACE

# What the caller does with a name: the text to put in its place, or None to leave it as written.
Render = Callable[[str], str | None]

# How many distinct names in one body are worth answering, in either direction. Without a limit
# anyone who can comment on the repository reaches every linked member of the server: a message
# trimmed to Discord's limit carried eighty-two live pings when it was measured. Names beyond the
# limit are still shown as written; what they lose is the notification.
MENTION_LIMIT = 10

# GitHub's own rule, narrower than "letters, digits and hyphens": a login may not begin or end
# with a hyphen or carry two in a row, so the loose version accepts `mona--lisa` and `monalisa-`,
# names GitHub cannot issue.
LOGIN = r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}"

# GitHub builds a team slug from the display name, so it takes underscores and full stops that an
# account name never would, and it can be longer.
TEAM_SLUG = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98})"

# `\A` and `\Z` rather than `^` and `$`: `$` also matches before a trailing newline, so `^...$`
# called `alice\n` a login. These validate what somebody types at `/link`, and what the transcript
# checks before putting a name in a URL.
_IS_LOGIN = re.compile(rf"\A{LOGIN}\Z")
_IS_TEAM_SLUG = re.compile(rf"\A{TEAM_SLUG}\Z")

# A slug in either shape: `_` is the one slug character `escape_markdown` touches and comes out
# as `\_`, which `_read` folds back. Against a raw body `names_in` stopped at `back` while the
# swap looked for `back_end`, the under-fetch that loses a mention in silence. The doubled
# backslash is load-bearing: `\_` also compiles and stops the slug at the first underscore.
_SLUG_IN_TEXT = r"[A-Za-z0-9](?:(?:[A-Za-z0-9._-]|\\_)*[A-Za-z0-9])?"


# What the preview puts where it cut the body short. A name against it is a prefix of somebody's
# name and not a name: `@monalisa` shows as `@mona`, and where a `mona` is also linked, rendering
# it would ping a person the comment never named.
_CUT_SHORT = "…"

# What may not come immediately before the `@`. The zero-width space is the only thing refusing
# `<@123>` and `<@&123>`, defused with the space between the `<` and the `@`; `@everyone`,
# `@here` and bare digits carry it after the `@` and are refused by a name having to start with a
# letter or a digit. `<` also refuses a live `<@7>`, since a login may be all digits and a note's
# own header line carries mentions this bot built; `/` keeps a name out of a pasted URL.
_BEFORE_A_NAME = f"(?<![A-Za-z0-9_@/<{ZERO_WIDTH_SPACE}-])"

# A code span is the one place GitHub does not read a mention, and issue #166 is the first time
# this module could respect that: a note's body used to arrive with its backticks escaped into
# literal characters, so there was no span left to see. Fenced blocks first, so one block is one
# span rather than three, and `.` crosses newlines for the same reason.
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


# The team alternative is first: an organisation can share a name with somebody who has linked an
# account, and trying the person first would ping them in their team's place.
_MENTION = re.compile(
    _BEFORE_A_NAME
    + rf"@(?:(?P<org>{LOGIN})/(?P<team>{_SLUG_IN_TEXT})(?![A-Za-z0-9{_CUT_SHORT}])"
    # The team guard refuses a trailing letter as well as the cut marker, which the user branch
    # gets for free. Without it the slug backtracks to a shorter one and `@canon/back…` matches a
    # team called `bac`.
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

    Two tuples rather than one: a slug that matches a login is not that person, and the two are
    looked up in different tables and written with different Discord syntax.
    """

    people: tuple[str, ...]
    teams: tuple[str, ...]


def _runs(text: str, *, skipping_code: bool) -> Iterator[tuple[str, bool]]:
    """The text in runs, and whether a name in each one counts.

    One split, shared by the reader and the swap, because the two have to answer the same question
    about the same characters. Everything else in this module is arranged around them agreeing;
    this is the only thing here that could make them disagree, so there is one of it.

    Safe for `_BEFORE_A_NAME`'s lookbehind, which is the thing a split like this usually breaks. A
    run can only begin where a span ended, so the character in front of it is always a backtick -
    and a backtick is not one of the characters that refuses a name. A split therefore never turns
    a refusal into a match, which the whole-string and split answers agreeing is what pins.
    """
    if not skipping_code:
        yield text, True
        return

    typed_from = 0
    for span in _CODE.finditer(text):
        yield text[typed_from : span.start()], True
        yield span.group(0), False
        typed_from = span.end()
    yield text[typed_from:], True


def _mentions_in(text: str, *, skipping_code: bool) -> Iterator[re.Match[str]]:
    """Every name-shaped thing worth reading, in the order it appears."""
    for run, counts in _runs(text, skipping_code=skipping_code):
        if counts:
            yield from _MENTION.finditer(run)


def names_in(text: str, *, skipping_code: bool = False) -> Mentioned:
    """Every name this body writes, up to the limit.

    Asked of the same text that will be shown: the preview is cut mid-word, so a body ending
    `@monalisa` shows as `@mona` and reading the whole body would ask about `monalisa` while the
    swap pings whoever is linked as `mona`. Asking too narrowly loses the mention in silence, and
    asking too widely is not merely wasteful - the budget below is shared, so a reader that looks
    further than the swap can spend it on names the swap will never reach.

    `skipping_code` leaves out the names inside a code span, which is what GitHub does and what a
    caller wants when the text still has its backticks. It has to match what the swap was asked.
    """
    people: list[str] = []
    teams: list[str] = []
    seen: set[tuple[bool, str]] = set()

    for match in _mentions_in(text, skipping_code=skipping_code):
        is_team, name = _read(match)
        key = (is_team, name)
        if key in seen:
            continue
        if len(seen) >= MENTION_LIMIT:
            break
        seen.add(key)
        (teams if is_team else people).append(name)

    return Mentioned(people=tuple(people), teams=tuple(teams))


def rewrite(text: str, *, person: Render, team: Render, skipping_code: bool = False) -> str:
    """Put each name the caller answers for in its place, and leave the rest as written.

    The budget counts distinct names, as `names_in` does, so the two reach the same distance
    into a body. Counting occurrences would rewrite names the lookup never asked about.

    `skipping_code` must match whatever the reader was asked, for the same reason the budget is
    counted the same way in both. Not the default, so the one other thing that reads with this
    module - the property asserting a published transcript mentions only the accounts it was handed
    - goes on being asked about every name-shaped thing in the text rather than a subset of them.
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

    return "".join(
        _MENTION.sub(swap, run) if counts else run
        for run, counts in _runs(text, skipping_code=skipping_code)
    )


def names_a_note_writes(text: str) -> Mentioned:
    """The names in a note's body, read the way a note's body has to be read.

    Its own name so that nothing has to remember a flag. `skipping_code` has to be the same
    answer on both sides or the reader and the swap disagree, and a disagreement between them is
    silent in both directions - so the pairing lives here, once, rather than at two call sites
    that are checked against each other by a test. `rewrite_a_note` below is the other half.
    """
    return names_in(text, skipping_code=True)


def rewrite_a_note(text: str, *, person: Render, team: Render) -> str:
    """The other half of `names_a_note_writes`, and the reason both exist."""
    return rewrite(text, person=person, team=team, skipping_code=True)


def _read(match: re.Match[str]) -> tuple[bool, str]:
    """Which kind of name this is, and the name itself, lowercased.

    The organisation in front of a team is dropped: `/link_team` stores a bare slug and the
    table is scoped to one server.
    """
    slug = match.group("team")
    if slug is not None:
        return True, slug.replace("\\_", "_").lower()
    return False, match.group("user").lower()
