"""The tier label, applied to the tier rather than to the files somebody remembered.

`integration` is on all 63 of its files; `unit` was on 32 of 88. So `-m unit` quietly meant
"about a third of the unit tests", which is worse than no selector at all: it reads as a gate and
is not one. Marking here is what makes the two markers mean the same kind of thing.

The hook is handed EVERY collected item, not only the ones under this directory, so the path test
is load-bearing. Without it this would mark the integration tier `unit` as well.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(pytest.mark.unit)
