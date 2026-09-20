"""Bringing one GitHub item into line with its Discord thread.

`items` is the path everything else serves: `staleness` decides whether a delivery is worth
believing, `threads` owns the gap between Discord creating a thread and the row knowing about it,
and `policies`, `notifications`, `one_at_a_time` and `manual` sit around them. `announcements`,
with `label_lines` and `state_lines`, carries what a delivery must say out loud, because a
rewritten block is silent in Discord.
"""
