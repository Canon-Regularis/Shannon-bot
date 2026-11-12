from __future__ import annotations

import re
from collections.abc import Iterable

from shannon.domain.enums import Priority

# Teams label priority in whatever style their repository already uses, so all are accepted.
_SEPARATORS = re.compile(r"[\s_\-:/]+")
_PREFIXES = ("priority", "prio", "p")
_SUFFIX = "priority"

# What a status label starts with since issue #246: `status: Ready To Merge`. Here rather than
# beside the rest of the label vocabulary in `github/labels.py`, which imports this module.
STATUS_PREFIX = "status:"

_WORDS: dict[str, Priority] = {
    "high": Priority.HIGH,
    "urgent": Priority.HIGH,
    "critical": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "med": Priority.MEDIUM,
    "moderate": Priority.MEDIUM,
    "low": Priority.LOW,
    "minor": Priority.LOW,
}

# HIGH outranks MEDIUM outranks LOW, so a mislabelled item is escalated rather than buried.
_RANK = {Priority.HIGH: 3, Priority.MEDIUM: 2, Priority.LOW: 1, Priority.UNSET: 0}


def parse_priority(label_names: Iterable[str]) -> Priority:
    """Work out an item's priority from its GitHub labels."""
    found = [priority for name in label_names if (priority := _from_label(name)) is not None]
    if not found:
        return Priority.UNSET
    return max(found, key=lambda priority: _RANK[priority])


def _from_label(name: str) -> Priority | None:
    # A status label is never a priority, whatever its column is called. Issue #246: a board's
    # column becomes `status: <column>`, and one named "High priority" would otherwise turn
    # every item moved into it urgent. The prefix exactly, as `labels.is_status_label` reads it,
    # so a team's own `status-high-priority` is still the priority it always was.
    if name.strip().casefold().startswith(STATUS_PREFIX):
        return None

    parts = [part for part in _SEPARATORS.split(name.strip().lower()) if part]
    if not parts:
        return None

    # "HIGH", on its own, is a priority label.
    if len(parts) == 1:
        return _WORDS.get(parts[0])

    # "priority: high", "p: high", "p-high" and "HIGH_PRIORITY". Numbered schemes like "P1" are
    # not read: which number means urgent differs between teams, and guessing wrong buries work.
    if parts[0] in _PREFIXES:
        return _WORDS.get(parts[1])
    if parts[-1] == _SUFFIX:
        return _WORDS.get(parts[-2])
    return None
