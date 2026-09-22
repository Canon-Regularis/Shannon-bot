from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import UserLink

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LinkedAccount:
    """What one Discord member claimed here: the login, and who GitHub said was holding it.

    Both halves or neither. The name on its own points at whoever holds it now, which is the
    whole reason the id is stored beside it, and answering with only the name is what made a
    renamed collaborator read as somebody with no access to the repository. Issue #133.

    Plain values rather than the row, because what comes between reading this and acting on it is
    a GitHub round trip, and a live row would hold a pooled connection open across it.
    """

    login: str
    github_user_id: int | None

    @classmethod
    def of(cls, row: UserLink) -> LinkedAccount:
        return cls(login=row.github_username, github_user_id=row.github_user_id)


class UserLinkStore:
    """Data access for the GitHub login to Discord account mapping, scoped to one guild.

    Logins are stored lowercased because GitHub treats them case insensitively.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> dict[str, int]:
        """Which of these people this server has a Discord account for, keyed by login.

        GitHub frees a renamed or deleted login for anybody to take, so a name-only match once gave
        a stranger the previous holder's review pings. A stored account id that disagrees with the
        one on the item is dropped and logged, since a stale link and a member who never linked look
        identical in the thread. A null on either side is no evidence — the row predates the column,
        or the payload carried no id — so the name still resolves.
        """
        wanted = {name.lower(): asked for name, asked in people.items()}
        if not wanted:
            return {}

        rows: Sequence[UserLink] = (
            await self._session.scalars(
                select(UserLink).where(
                    UserLink.discord_guild_id == guild_id,
                    UserLink.github_username.in_(wanted),
                )
            )
        ).all()

        resolved: dict[str, int] = {}
        for row in rows:
            asked = wanted[row.github_username]
            if row.github_user_id is not None and asked is not None and row.github_user_id != asked:
                logger.warning(
                    "%r is linked here to the account %s, and the one on this item is %s, so it "
                    "is somebody else now and will not be mentioned; that person should run "
                    "/link again",
                    row.github_username,
                    row.github_user_id,
                    asked,
                )
                continue
            resolved[row.github_username] = row.discord_user_id
        return resolved

    async def account_for(self, *, guild_id: int, discord_user_id: int) -> LinkedAccount | None:
        """What this Discord member claimed here, or None if they never claimed anything.

        Both halves, because the login alone is not enough to write to GitHub with. This used to
        answer with the name and a note saying there was no second id to hold it against; there
        was, one column over, and the cost of not reading it was a collaborator being told he had
        no access to a repository he could write to. Issue #133.

        At most one row matches: `uq_user_links_guild_discord` is unique on this pair and its
        index serves the lookup.
        """
        row = await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.discord_user_id == discord_user_id,
            )
        )
        return None if row is None else LinkedAccount.of(row)

    async def follow_rename(
        self, *, guild_id: int, discord_user_id: int, github_user_id: int, login: str
    ) -> None:
        """Take the login GitHub is using for this account now, where nothing is in the way.

        Named for `RepositoryStore.follow_rename`, which is the same move made for a repository:
        the id is the identity and the name is a label GitHub reassigns, so following it is how a
        stored name stays true. Called only when the two have actually been seen to differ, so
        this never touches a row for a command that changed nothing.

        The same lock `link` takes, keyed the same way, because the check below races it
        otherwise. Taken here rather than around the GitHub call that found the new name: holding
        a lock across a network round trip would serialise a guild on somebody else's latency.
        """
        await self._session.execute(select(func.pg_advisory_xact_lock(guild_id)))
        username = login.lower()

        row = await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.discord_user_id == discord_user_id,
                UserLink.github_user_id == github_user_id,
            )
        )
        if row is None:
            # Somebody re-ran `/link` for them between the read and here. That is a claim somebody
            # made deliberately and this is a correction made in passing, so the claim wins.
            logger.info(
                "the link for discord user %s in guild %s moved while %s was being followed",
                discord_user_id,
                guild_id,
                username,
            )
            return

        held = await self._session.scalar(
            select(UserLink.discord_user_id).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.github_username == username,
                UserLink.id != row.id,
            )
        )
        if held is not None:
            # `link` would delete the row in the way, and that is its prerogative: it runs when
            # somebody states a claim. This runs inside `/assign`, so a developer putting a
            # colleague on an issue must not silently destroy a third person's link on the way
            # past. Said out loud instead, because a collision means one of the two rows is wrong.
            logger.warning(
                "discord user %s in guild %s is now %s on GitHub, which discord user %s is "
                "already linked to; leaving both alone, and one of them wants relinking",
                discord_user_id,
                guild_id,
                username,
                held,
            )
            return

        logger.info(
            "discord user %s in guild %s is now %s on GitHub, following the rename from %s",
            discord_user_id,
            guild_id,
            username,
            row.github_username,
        )
        row.github_username = username
        await self._session.flush()

    async def logins_for(
        self, *, guild_id: int, discord_user_ids: Collection[int]
    ) -> dict[int, str]:
        """Which GitHub account each of these Discord members claimed here, keyed by Discord id.

        `account_for` for a batch, and deliberately less careful than it: this answers with the
        claims as they were made, so a login somebody freed and a stranger took is answered with
        the stranger. That is the right trade here and not there. Its caller renders names into a
        published transcript rather than writing to GitHub, where the cost of being wrong is a
        name reading oddly rather than a stranger put on somebody's pull request, and asking
        GitHub about every person every publish would be a call each.

        An id nobody linked is absent from the result.
        """
        wanted = set(discord_user_ids)
        if not wanted:
            return {}

        rows = (
            await self._session.execute(
                select(UserLink.discord_user_id, UserLink.github_username).where(
                    UserLink.discord_guild_id == guild_id,
                    UserLink.discord_user_id.in_(wanted),
                )
            )
        ).all()
        return {discord_user_id: login for discord_user_id, login in rows}

    async def link(
        self,
        *,
        guild_id: int,
        github_username: str,
        github_user_id: int | None,
        discord_user_id: int,
    ) -> UserLink:
        """Bind a GitHub login to a Discord account, replacing whatever either side had.

        Both halves are unique within a guild and can be held by two different rows, so editing one
        in place collides with the other: clear both and insert, as one step. An upsert cannot do it
        — a row can conflict on either constraint and `ON CONFLICT` names one — and nor can a retry
        loop, since the retries collide with each other. Hence the advisory lock, keyed per guild;
        the bare guild id is safe because the project's other lock passes Postgres two integers
        rather than one bigint, and those spaces stay apart.
        """
        await self._session.execute(select(func.pg_advisory_xact_lock(guild_id)))

        username = github_username.lower()

        existing = await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.github_username == username,
                UserLink.discord_user_id == discord_user_id,
            )
        )
        if existing is not None:
            # The id is still worth writing: a row from before that column carries none, and
            # re-running `/link` is the only way one is ever filled in, since GitHub can say what a
            # login is called now and not what it was called when somebody bound it.
            existing.github_user_id = github_user_id
            return existing

        await self._session.execute(
            delete(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                or_(
                    UserLink.github_username == username,
                    UserLink.discord_user_id == discord_user_id,
                ),
            )
        )

        link = UserLink(
            discord_guild_id=guild_id,
            github_username=username,
            github_user_id=github_user_id,
            discord_user_id=discord_user_id,
        )
        self._session.add(link)
        await self._session.flush()
        return link
