"""Bringing one GitHub item into line with its Discord thread.

`items` is the path everything else here serves: the policies say how a pull request differs
from an issue, `staleness` decides whether a delivery is worth believing, `threads` owns the gap
between Discord creating a thread and the row knowing about it, `notifications` claims a ping
before sending it, and `manual` is the same sync driven by a command instead of a webhook.
"""

from shannon.services.sync.items import (
    ItemSyncService,
    SyncOutcome,
    SyncResult,
    build_item_handler,
)

__all__ = ["ItemSyncService", "SyncOutcome", "SyncResult", "build_item_handler"]
