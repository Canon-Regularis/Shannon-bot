"""Keeping the map from a GitHub account to the App's installation on it.

In services rather than in `github`, because the map is a table: what GitHub says about an
installation is read in `github`, and writing it down is the database's business (#271).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.domain.json import JsonObject
from shannon.github.installations import Knows, MapSays
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.github.webhooks.installations import parse_installation_event

logger = logging.getLogger(__name__)

# Somebody removed the App. `installation_repositories.removed` is deliberately not here:
# taking a repository out of an installation leaves the installation standing, and forgetting it
# would break every other repository under that account.
GONE = "deleted"
SUSPENDED = "suspend"
UNSUSPENDED = "unsuspend"

# What an action says about the suspension, for the two that say anything. Every other one is
# None, which the store reads as "no word on it" and leaves what it holds: `added` or
# `new_permissions_accepted` used to write False and clear a suspension, the #268 bug from the
# webhook's side (#271). Not read off the payload's `suspended_at`, because a delivery GitHub
# sends again carries the value as it was when it first went out.
_SAYS_SUSPENDED = {SUSPENDED: True, UNSUSPENDED: False}


class InstallationDirectory:
    """Owner to installation, out of the database.

    No in-process cache, so a token costs one query whether or not it is already held. On purpose:
    a suspend or an uninstall is then honoured by the next call rather than when a cache lapses.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def installation_for(self, owner: str) -> MapSays:
        async with self._sessionmaker() as session:
            found = await InstallationStore(session).for_owner(owner)
        if found is None:
            return MapSays(Knows.NOTHING)
        if found.suspended:
            # Suspended is not uninstalled, and minting against a suspended installation
            # fails, so there is nothing to be gained by trying.
            logger.info("the installation for %s is suspended, so nothing can be read", owner)
            return MapSays(Knows.A_SUSPENSION)
        return MapSays(Knows.AN_INSTALLATION, found.installation_id)

    async def remember(
        self,
        *,
        installation_id: int,
        account_login: str,
        account_id: int | None,
        suspended: bool,
    ) -> None:
        """Write down an installation this bot had not been told about.

        `for_owner` has said since it was written that None means "ask GitHub" rather than
        "not installed". Nothing asked, and nothing could keep the answer if it had. This is
        the keeping half.
        """
        async with self._sessionmaker() as session, session.begin():
            await InstallationStore(session).remember(
                installation_id=installation_id,
                account_login=account_login,
                account_id=account_id,
                suspended=suspended,
            )

    async def forget(self, installation_id: int) -> None:
        """Drop a row GitHub will not honour, so the next call can learn what replaced it.

        A reinstall keeps the login and issues a NEW installation id, so a row holding the old one
        is worse than no row: it answers `AN_INSTALLATION`, the asking never happens, and every
        call posts a mint GitHub answers 404 - which caches nothing, because there is no token to
        cache. Forgetting it turns the next call into a question, and a question is the one thing
        `InstallationTokens` can now answer.
        """
        async with self._sessionmaker() as session, session.begin():
            await InstallationStore(session).forget(installation_id)


def build_installation_handler(sessionmaker: async_sessionmaker[AsyncSession]) -> EventHandler:
    """Keep the installation directory in step with what GitHub says about itself."""

    async def handle(
        action: str, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        found = parse_installation_event(payload)
        if found is None:
            logger.warning("installation.%s arrived without a usable installation", action)
            return WebhookOutcome.IGNORED

        async with sessionmaker() as session, session.begin():
            store = InstallationStore(session)
            if action == GONE:
                removed = await store.forget(found.installation_id)
                logger.info(
                    "the app was removed from %s%s",
                    found.account_login,
                    "" if removed else ", which it was not installed on here",
                )
                return WebhookOutcome.PROCESSED

            # A row for a suspend as well, so one for an account this bot never saw still leaves
            # one behind: the App was installed while this process was down.
            await store.remember(
                installation_id=found.installation_id,
                account_login=found.account_login,
                account_id=found.account_id,
                suspended=_SAYS_SUSPENDED.get(action),
            )

        logger.info(
            "the app is installed on %s as installation %s%s",
            found.account_login,
            found.installation_id,
            " and is suspended" if action == SUSPENDED else "",
        )
        return WebhookOutcome.PROCESSED

    return handle
