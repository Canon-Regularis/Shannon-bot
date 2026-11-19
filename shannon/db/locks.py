"""Every advisory-lock key space this project takes, in one place.

Postgres has one advisory-lock namespace per database, shared by everything that connects, so two
locks that happen to pick the same key serialise each other with nothing to say why. Each module
used to defend its own key with a comment naming the others, and those comments had already
drifted apart. A new lock adds its key space here. Issue #268.

Why the spaces in use cannot collide:

- The one-bigint form, `pg_advisory_xact_lock(key)`, is the guild id, and `hold_guild` is the only
  thing that takes it.
- The two-integer form, `pg_advisory_xact_lock(first, second)`, leads with one of the constants
  below. Postgres records the two forms apart (`objsubid` 1 and 2 in `pg_locks`), so no guild id
  can ever equal a two-integer key, whatever its value.
- Two-integer spaces stay apart from each other because each has its own first key, and
  `test_the_lock_spaces_never_share_a_key` holds them distinct.

Plain ints rather than an enum, so what is bound is exactly the value it always was and the locks
are the same locks they were before this module existed.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# One writer at a time per tracked item, around a whole sync (`services.sync.one_at_a_time`).
ONE_ITEM_AT_A_TIME: Final = 8_531

# One writer at a time per GitHub account in the installation map (`stores.installations`).
ONE_ACCOUNT_AT_A_TIME: Final = 8_532


async def hold_guild(session: AsyncSession, guild_id: int) -> None:
    """Hold the guild's link lock until this transaction ends.

    The one-bigint space, keyed on the guild id itself. A function rather than a constant because
    this space has no first key to name, and `UserLinkStore` takes it from two places.
    """
    await session.execute(select(func.pg_advisory_xact_lock(guild_id)))
