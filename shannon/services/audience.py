"""Who a message is for, turned into what Discord needs to reach them.

Two things ask this and get the same answer: a CI result, which tells whoever a broken build
concerns, and a pull request leaving draft, which tells whoever it is now waiting on. Both need
the same three reads, and each of those reads is done the way it is for a reason worth writing
down once rather than twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import Notify
from shannon.domain.models import Actor


@dataclass(frozen=True, slots=True)
class Audience:
    """The people a message may name, and which of them it may ring.

    Named fields rather than the three-tuple this replaces, because the third is not a third
    mapping of the same kind: `notify` is the allow-list handed to Discord, and a caller that
    passed it where `mentions` belongs would build a message naming nobody and ringing nobody
    without failing anywhere a test would see.
    """

    mentions: dict[str, int]
    roles: dict[str, int]
    notify: Notify


async def reachable(
    session: AsyncSession,
    *,
    guild_id: int,
    people: Sequence[Actor],
    teams: Sequence[Actor],
) -> Audience:
    """Names into mentions, and the allow-list that decides which of them ring.

    The ids travel with the logins because GitHub frees a login when an account is renamed or
    deleted, and without the id a mention meant for one person reaches whoever took the name.
    Roles are absent from the allow-list on purpose: Discord rings everybody holding a role and a
    member cannot opt out of one, so putting a role id there would claim a control that does not
    exist.

    Takes a session rather than a sessionmaker, so a caller already inside one is not made to open
    a second. Everything here is a read.
    """
    mentions = await UserLinkStore(session).resolve_many(
        guild_id=guild_id, people={person.login: person.github_user_id for person in people}
    )
    # No empty-mapping guard on either: both stores answer one without asking the database.
    roles = await TeamLinkStore(session).resolve_many(
        guild_id=guild_id, people=dict.fromkeys((team.login for team in teams), None)
    )
    notify = await MutedMemberStore(session).may_be_pinged(guild_id=guild_id, ids=mentions.values())
    return Audience(mentions=dict(mentions), roles=dict(roles), notify=notify)
