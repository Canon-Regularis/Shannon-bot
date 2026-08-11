from __future__ import annotations

import re
from collections.abc import Iterable

from shannon.domain.enums import Priority

# Teams label priority in whatever style their repository already uses, so all of these are
# accepted rather than forcing one spelling on them.
_SEPARATORS = re.compile(r"[\s_\-:/]+")
_PREFIXES = ("priority", "prio", "p")
_SUFFIX = "priority"

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
    """Work out an item's priority from its GitHub labels.

    Returns UNSET when no label says anything about priority. When several do, the highest
    wins.
    """
    found = [priority for name in label_names if (priority := _from_label(name)) is not None]
    if not found:
        return Priority.UNSET
    return max(found, key=lambda priority: _RANK[priority])


def _from_label(name: str) -> Priority | None:
    parts = [part for part in _SEPARATORS.split(name.strip().lower()) if part]
    if not parts:
        return None

    # "HIGH", on its own, is a priority label.
    if len(parts) == 1:
        return _WORDS.get(parts[0])

    # "priority: high", "P1"-style prefixes, and "HIGH_PRIORITY" suffixes.
    if parts[0] in _PREFIXES:
        return _WORDS.get(parts[1])
    if parts[-1] == _SUFFIX:
        return _WORDS.get(parts[-2])
    return None
