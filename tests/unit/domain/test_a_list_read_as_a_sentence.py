"""`listed`, the one "A, B or C" join `/remind`, `/track` and the workflow commands share. #267.

Pinned here rather than left to the commands' own tests, so the branch coverage of
`domain/text.py` does not hang on which lists those commands happen to build.
"""

from __future__ import annotations

import pytest

from shannon.domain.text import listed


class TestAListReadAsASentence:
    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            ([], ""),
            (["A"], "A"),
            (["A", "B"], "A and B"),
            (["A", "B", "C"], "A, B and C"),
            (["A", "B", "C", "D"], "A, B, C and D"),
        ],
        ids=["none", "one", "two", "three", "four"],
    )
    def test_the_last_two_are_joined_with_and_by_default(
        self, names: list[str], expected: str
    ) -> None:
        assert listed(names) == expected

    def test_the_last_two_can_be_joined_with_or(self) -> None:
        assert listed(["A", "B"], last="or") == "A or B"
        assert listed(["A", "B", "C"], last="or") == "A, B or C"

    def test_one_name_never_reads_as_half_a_choice(self) -> None:
        """`/track`'s own copy gave " or A" for one name, and held it off only by a comment."""
        assert listed(["A"], last="or") == "A"

    def test_a_tuple_is_taken_as_it_is(self) -> None:
        """The workflow commands' own names are a tuple, so no copy into a list is needed."""
        assert listed(("A", "B", "C"), last="or") == "A, B or C"
