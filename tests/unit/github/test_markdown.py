"""What a table's refusal says, so whoever reads it can find the line break.

Issue #271. The only caller passes constant names and varying values, so the break is nearly
always in a value; a message naming only the row's name pointed at a cell with nothing wrong in
it. The rest of the module's behaviour is pinned in `test_github_markdown.py`.
"""

from __future__ import annotations

import pytest

from shannon.github.markdown import table

pytestmark = pytest.mark.unit


class TestARefusedTable:
    def test_a_break_in_the_value_is_named_with_the_value_and_its_row(self) -> None:
        with pytest.raises(ValueError, match="line break") as refused:
            table([("Participants", "ada\nbob")])

        assert repr("ada\nbob") in str(refused.value)
        assert repr("Participants") in str(refused.value)

    def test_a_break_in_the_name_is_named_with_the_name(self) -> None:
        with pytest.raises(ValueError, match="line break") as refused:
            table([("Source\nThread", "Discord")])

        assert repr("Source\nThread") in str(refused.value)
        assert repr("Discord") not in str(refused.value)

    def test_only_the_row_with_the_break_is_named(self) -> None:
        with pytest.raises(ValueError) as refused:
            table([("Source", "Discord"), ("Thread", "a\nb")])

        assert repr("Source") not in str(refused.value)
        assert repr("Thread") in str(refused.value)
