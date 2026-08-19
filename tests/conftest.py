from __future__ import annotations

from hypothesis import settings

# Hypothesis fails an example that runs longer than its deadline. That measures the machine
# rather than the property: the first call into a module pays for importing it, and this shares
# a box with the integration tier. What these tests assert is what holds, not how fast.
settings.register_profile("shannon", deadline=None)
settings.load_profile("shannon")
