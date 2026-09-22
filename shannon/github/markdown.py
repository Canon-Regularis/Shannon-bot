"""Building the containers a GitHub comment is made of.

The other half of `safe_text`, which keeps text from escaping: this puts it somewhere. The two
are split the way `discord_bot/panels.py` and `discord_bot/layout.py` are, and for the same
reason — what a comment says is decided in one place and how it is framed in another.

Every function here takes text that is ALREADY SAFE. Nothing in this module defuses anything:
a caller hands over the output of `as_inline_text` for a name, or of `one_message` for something
somebody typed, and these decide only where it goes. Splitting it that way is what keeps the
escaping rules in one file rather than spread across every construct that has to obey them.
"""

from __future__ import annotations

from collections.abc import Sequence

# A cell holds one line. A newline inside one ends the row, and every later cell shifts up a
# column, so a name with a line break in it would rewrite the table rather than sit in it.
_NOTHING = "—"


def table(rows: Sequence[tuple[str, str]]) -> str:
    """A two-column table of already-safe values.

    Headerless: GitHub needs the separator row to read it as a table at all, but an empty header
    renders as nothing and the left column is doing the naming already.

    A newline in a value would end the row and shift every later cell up a column, so one is
    refused rather than rendered into a broken table. Callers pass names through
    `as_inline_text`, which escapes the pipe; this is the guard for the hazard that escaping
    cannot reach.
    """
    for name, value in rows:
        if "\n" in name or "\n" in value:
            raise ValueError(f"a table cell cannot hold a line break: {name!r}")

    lines = ["|  |  |", "|---|---|"]
    lines.extend(f"| **{name}** | {value or _NOTHING} |" for name, value in rows)
    return "\n".join(lines)


def details(summary: str, body: str, *, expanded: bool) -> str:
    """A collapsible block holding already-safe markdown.

    The blank lines around the body are load-bearing: GitHub stops reading markdown inside an
    HTML block unless one separates them, so without it the whole thread renders as one run of
    literal text.

    `expanded` rather than `open`, which is a builtin.
    """
    tag = "<details open>" if expanded else "<details>"
    return f"{tag}\n<summary><strong>{summary}</strong></summary>\n\n{body}\n\n</details>"


def note(text: str) -> str:
    """A GitHub note callout carrying already-safe text.

    Every line is prefixed, including the blank ones: a bare line ends the quote, and the rest
    of the callout then renders as ordinary text below it.
    """
    quoted = "\n".join(f"> {line}".rstrip() for line in text.splitlines())
    return f"> [!NOTE]\n{quoted}"


def link(label: str, url: str) -> str:
    """A link whose label is already safe and whose URL the caller has already built.

    No escaping here on purpose. A label still carrying a `]` would end the link early and a URL
    still carrying a `)` would end the target, so both are the caller's to have made safe —
    `as_inline_text` for the label, and for the URL a value that was never text in the first
    place, like an id or a login `is_login` has passed.
    """
    return f"[{label}]({url})"
