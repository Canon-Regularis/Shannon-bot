"""The advisory-lock key spaces, held apart by `shannon.db.locks`. Issue #268.

Two locks that pick the same key serialise each other with nothing to say why, so a new space
that reused a first key would only show up as a sync waiting on an unrelated `/register`.
"""

from __future__ import annotations

import pytest

from shannon.db import locks

pytestmark = pytest.mark.unit

# The bounds of a Postgres `integer`, which is what each half of a two-integer key is.
INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


def first_keys() -> dict[str, int]:
    """Every first key the registry declares, found rather than listed, so a new one is held to
    the same rules without anybody remembering to add it here."""
    return {
        name: value
        for name, value in vars(locks).items()
        if name.isupper() and isinstance(value, int)
    }


def test_the_lock_spaces_never_share_a_key() -> None:
    keys = first_keys()

    assert len(set(keys.values())) == len(keys), f"two lock spaces share a first key: {keys}"


def test_the_spaces_in_use_are_the_ones_the_locks_have_always_taken() -> None:
    """Moved here from their own modules, not renumbered. A changed value is a different lock,
    and a deploy that runs old and new side by side would hold two writers apart on nothing."""
    assert first_keys() == {"ONE_ITEM_AT_A_TIME": 8_531, "ONE_ACCOUNT_AT_A_TIME": 8_532}


def test_every_first_key_fits_the_integer_postgres_takes() -> None:
    assert all(INT4_MIN <= value <= INT4_MAX for value in first_keys().values())
