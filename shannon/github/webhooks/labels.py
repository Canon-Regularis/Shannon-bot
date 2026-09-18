"""Which label a delivery moved, for the two actions that move one."""

from __future__ import annotations

import logging

from shannon.domain.json import JsonObject, is_json_object
from shannon.domain.models import LabelMove
from shannon.domain.priority import parse_priority
from shannon.github.labels import status_of

logger = logging.getLogger(__name__)

# The two actions that name a label at the top level of the delivery. Every other action carries
# the item's whole label list and nothing about what changed, which is what the metadata block
# reads; only these two can say which one moved.
LABEL_ACTIONS = frozenset({"labeled", "unlabeled"})


def parse_label_move(action: str, payload: JsonObject) -> LabelMove | None:
    """Which label this delivery put on or took off, or None where it says nothing about one.

    Read off the delivery rather than worked out by comparing label lists, because GitHub has
    already done the work: it sends one delivery per label with that label at the top level, so
    there is nothing to diff and nothing to store. Comparing lists instead would mean keeping
    the previous set on the row, and would still be wrong for the first event after a restart.

    None rather than an exception for anything unexpected, because this decides whether to say
    something and nothing else. A delivery this cannot read is one the item sync still handles
    in full; the thread keeps its metadata block and loses only the line announcing the change.

    The label is classified here, at the one place a move is built, rather than wherever a move
    is read. Both classifiers already exist and both are asked unconditionally, so this costs
    two calls over a name GitHub caps at fifty characters and no branch at all.
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
