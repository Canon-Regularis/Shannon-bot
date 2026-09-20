"""Following the App being installed, removed, paused or resumed.

The only events this bot handles that are not about an item: they change what it can see at all.
What they maintain is the account-to-installation map, and that map is a cache with GitHub behind
it, so a missed delivery costs a lookup rather than a broken mirror.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.domain.json import JsonObject, is_json_object
from shannon.github.webhooks.events import EventHandler, WebhookOutcome

logger = logging.getLogger(__name__)

# Somebody removed the App. `installation_repositories.removed` is deliberately not here:
# taking a repository out of an installation leaves the installation standing, and forgetting it
# would break every other repository under that account.
GONE = "deleted"
SUSPENDED = "suspend"


@dataclass(frozen=True, slots=True)
class InstallationEvent:
    """What one installation delivery says, once the fields that matter are checked."""

    installation_id: int
    account_login: str
    account_id: int | None


def parse_installation_event(payload: Any) -> InstallationEvent | None:
    """The installation out of any delivery that carries one, or None for one that does not.

    Every App delivery carries the `installation` block, an `issues` one as much as an
    `installation` one, so a directory that missed a webhook repairs itself from ordinary traffic.
    The account id is optional: a row recording a login with no id is still useful.
    """
    if not is_json_object(payload):
        return None
    installation = payload.get("installation")
    if not is_json_object(installation):
        return None

    installation_id = installation.get("id")
    if not isinstance(installation_id, int):
        return None

    account = installation.get("account")
    login = account.get("login") if is_json_object(account) else None
    if not isinstance(login, str) or not login:
        return None

    account_id = account.get("id") if is_json_object(account) else None
    return InstallationEvent(
        installation_id=installation_id,
        account_login=login,
        account_id=account_id if isinstance(account_id, int) else None,
    )


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

            # Written before the suspension is applied, so a suspend for an account this
            # bot never saw still leaves a row behind: the App was installed while this process
            # was down.
            await store.remember(
                installation_id=found.installation_id,
                account_login=found.account_login,
                account_id=found.account_id,
                suspended=action == SUSPENDED,
            )

        logger.info(
            "the app is installed on %s as installation %s%s",
            found.account_login,
            found.installation_id,
            " and is suspended" if action == SUSPENDED else "",
        )
        return WebhookOutcome.PROCESSED

    return handle
