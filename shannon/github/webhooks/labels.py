"""Which label a delivery moved, for the two actions that move one."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shannon.domain.models import LabelMove

logger = logging.getLogger(__name__)

# The two actions that name a label at the top level of the delivery. Every other action carries
# the item's whole label list and nothing about what changed, which is what the metadata block
# reads; only these two can say which one moved.
LABEL_ACTIONS = frozenset({"labeled", "unlabeled"})


def parse_label_move(action: str, payload: Mapping[str, Any]) -> LabelMove | None:
    """Which label this delivery put on or took off, or None where it says nothing about one.

    Read off the delivery rather than worked out by comparing label lists, because GitHub has
    already done the work: it sends one delivery per label with that label at the top level, so
    there is nothing to diff and nothing to store. Comparing lists instead would mean keeping
    the previous set on the row, and would still be wrong for the first event after a restart.

    None rather than an exception for anything unexpected, because this decides whether to say
    something and nothing else. A delivery this cannot read is one the item sync still handles
    in full; the thread keeps its metadata block and loses only the line announcing the change.
    """
    if action not in LABEL_ACTIONS:
        return None

    label = payload.get("label")
    if not isinstance(label, Mapping):
        logger.info("%s arrived without a label object, so nothing is said about it", action)
        return None

    name = label.get("name")
    if not isinstance(name, str) or not name:
        logger.info("%s arrived with an unusable label name, so nothing is said about it", action)
        return None

    return LabelMove(name=name, added=action == "labeled")
