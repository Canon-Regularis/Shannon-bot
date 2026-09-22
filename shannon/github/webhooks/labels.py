"""Which label a delivery moved, for the two actions that move one."""

from __future__ import annotations

import logging

from shannon.domain.json import JsonObject, is_json_object
from shannon.domain.models import LabelMove
from shannon.domain.priority import parse_priority
from shannon.github.labels import status_of

logger = logging.getLogger(__name__)

# The two actions that name a label at the top level of the delivery. Every other action carries
# the item's whole label list and nothing about what changed.
LABEL_ACTIONS = frozenset({"labeled", "unlabeled"})


def parse_label_move(action: str, payload: JsonObject) -> LabelMove | None:
    """Which label this delivery put on or took off, or None where it says nothing about one.

    GitHub sends one delivery per label with that label at the top level, so there is nothing to
    diff and no previous set to store. None rather than an exception: a delivery this cannot read
    is one the item sync still handles in full, and the thread loses only the announcing line.
    """
    if action not in LABEL_ACTIONS:
        return None

    label = payload.get("label")
    if not is_json_object(label):
        logger.info("%s arrived without a label object, so nothing is said about it", action)
        return None

    name = label.get("name")
    if not isinstance(name, str) or not name:
        logger.info("%s arrived with an unusable label name, so nothing is said about it", action)
        return None

    return LabelMove(
        name=name,
        added=action == "labeled",
        priority=parse_priority([name]),
        status=status_of([name]),
    )
