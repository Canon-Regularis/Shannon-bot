"""Bringing one GitHub item into line with its Discord thread.

`items` is the path everything else here serves: the policies say how a pull request differs
from an issue, `staleness` decides whether a delivery is worth believing, `threads` owns the gap
between Discord creating a thread and the row knowing about it, `notifications` claims a ping
before sending it, `one_at_a_time` holds one item to one writer across its Discord calls, and
`manual` is the same sync driven by a command instead of a webhook.

`announcements` is the seam for everything a delivery has to say out loud, because a rewritten
block is silent in Discord. `label_lines` and `state_lines` are the two that use it.
"""
