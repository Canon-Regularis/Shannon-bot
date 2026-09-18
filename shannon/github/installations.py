"""Turning "which account is this call about" into a token that can see it.

The second half of App authentication. `app_auth` signs the JWT that proves this process is the
App; this trades that JWT for a token scoped to one installation, and keeps it until it expires.

Two protocols rather than one class, because the two halves fail differently and are stubbed
differently. Resolving an owner is a database read with a network fallback; minting is a network
call with a cache in front of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.domain.json import JsonObject, is_json_object
from shannon.github.app_auth import app_jwt
from shannon.github.errors import GitHubAuthError
from shannon.github.mapping import parse_timestamp

logger = logging.getLogger(__name__)

# How long before a token expires it is thrown away and minted again. GitHub gives an hour, and a
# request that leaves here with fifty-nine minutes on it is fine; one that leaves with two seconds
# may arrive expired. Five minutes covers a slow request, a retry behind it and a clock that
# disagrees, and costs one extra mint every twelve hours.
REFRESH_MARGIN = timedelta(minutes=5)


class ResolvesInstallations(Protocol):
    """Which installation covers a GitHub account, or None if none does."""

    async def installation_for(self, owner: str) -> int | None: ...


class InstallationDirectory:
    """Owner to installation, out of the database.

    No in-process cache. The token cache in front of this already absorbs the hot path, so this
    runs about once per account per hour, and a cache with no invalidation story is worth more
    trouble than one small query.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def installation_for(self, owner: str) -> int | None:
        async with self._sessionmaker() as session:
            found = await InstallationStore(session).for_owner(owner)
        if found is None:
            return None
        if found.suspended:
            # Suspended is not uninstalled, and the difference matters to whoever has to fix it.
            # Minting against it fails, so there is nothing to be gained by trying.
            logger.info("the installation for %s is suspended, so nothing can be read", owner)
            return None
        return found.installation_id


class InstallationTokens:
    """Mints installation tokens and keeps each one until it is nearly stale.

    One lock per installation rather than one for everything. A burst of deliveries for one
    repository arrives together and must mint once between them; a burst across two repositories
    has no reason to queue behind each other.
    """

    def __init__(
        self,
        *,
        client_id: str,
        private_key_pem: str,
        http: httpx.AsyncClient,
        directory: ResolvesInstallations,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_id = client_id
        self._private_key_pem = private_key_pem
        self._http = http
        self._directory = directory
        self._now = now
        self._minted: dict[int, tuple[str, datetime]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # None until asked for, "" once asked and not answered. The two are different: the second
        # stops a failing read being retried on every `/register`.
        self._slug: str | None = None

    def app_token(self) -> str:
        """The App's own JWT, for the handful of endpoints that take one rather than a token."""
        return app_jwt(
            client_id=self._client_id, private_key_pem=self._private_key_pem, now=self._now()
        )

    async def token_for(self, owner: str) -> str:
        if not self.app_token():
            return ""

        installation = await self._directory.installation_for(owner)
        if installation is None:
            return ""

        held = self._minted.get(installation)
        if held is not None and held[1] - REFRESH_MARGIN > self._now():
            return held[0]

        lock = self._locks.setdefault(installation, asyncio.Lock())
        async with lock:
            # Checked again inside the lock. Ten deliveries arriving together all miss the cache
            # above, and without this they queue up and mint ten tokens, nine of which are thrown
            # away and every one of which counts against the App's rate limit.
            held = self._minted.get(installation)
            if held is not None and held[1] - REFRESH_MARGIN > self._now():
                return held[0]
            return await self._mint(installation, owner)

    async def installed_on(self, owner: str, name: str) -> int | None:
        """Which installation covers one repository, asked of GitHub rather than of the cache.

        The question `/register` needs and the directory cannot answer: a repository nobody has
        registered yet has no row, and "no row" deliberately means "ask GitHub" rather than "not
        installed". This is that ask.

        A JWT rather than a token, because there is no token to use until this has answered.

        404 is the whole point of the call. GitHub answers it both for a repository that does not
        exist and for one the App was never installed on, and the two are the same thing from
        here: this bot cannot see it, and somebody has to go and install the App. Saying which of
        the two it is would also tell anybody who asked whether a private repository exists.
        """
        if not self.app_token():
            return None

        response = await self._http.get(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/installation",
            headers={"Authorization": f"Bearer {self.app_token()}"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubAuthError(
                f"GitHub refused to say whether the app is installed on {owner}/{name} "
                f"({response.status_code}). Check the GitHub App's id and private key."
            )

        payload = _body(response)
        installation = payload.get("id")
        return installation if isinstance(installation, int) else None

    async def app_slug(self) -> str:
        """The App's own slug, for building the "install me" link, read once and kept.

        Read from GitHub rather than configured. It never changes for a given App, the JWT already
        proves which App is asking, and a setting for it would be one more thing to type wrong in
        a way whose only symptom is a link that 404s.
        """
        if self._slug is not None:
            return self._slug
        if not self.app_token():
            return ""

        response = await self._http.get(
            "/app", headers={"Authorization": f"Bearer {self.app_token()}"}
        )
        if response.status_code >= 400:
            # Not raised. The slug is only ever used to make a message more helpful, and failing
            # `/register` because the link could not be prettified would be the wrong trade.
            logger.warning("could not read the app's own slug (%s)", response.status_code)
            return ""

        slug = _body(response).get("slug")
        self._slug = slug if isinstance(slug, str) else ""
        return self._slug

    async def _mint(self, installation: int, owner: str) -> str:
        """Ask GitHub for a token, or answer "" for an installation that has gone.

        A 404 means the installation was removed between the directory read and this call, which
        is uninstalling: permanent, and the same outcome as never having been installed. The row
        is dropped so the next call does not pay for the round trip again.

        Everything else raises. A 401 is a key GitHub will not accept, and reporting that as "no
        installation" would send somebody looking at the wrong thing entirely.
        """
        response = await self._http.post(
            f"/app/installations/{installation}/access_tokens",
            headers={"Authorization": f"Bearer {self.app_token()}"},
        )
        if response.status_code == 404:
            logger.info("GitHub has no installation %s for %s any more", installation, owner)
            self._minted.pop(installation, None)
            return ""
        if response.status_code >= 400:
            raise GitHubAuthError(
                f"GitHub refused an installation token for {owner} "
                f"({response.status_code}). Check the GitHub App's id and private key."
            )

        token, expires_at = _minted(response)
        self._minted[installation] = (token, expires_at)
        return token


def _body(response: httpx.Response) -> JsonObject:
    """A JSON object, or an empty one. Used where a missing field is already handled below."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}
    return payload if is_json_object(payload) else {}


def _minted(response: httpx.Response) -> tuple[str, datetime]:
    """The token and its expiry, refusing a body that is missing either.

    Refused rather than defaulted, because both plausible defaults are wrong in a way that lasts.
    An assumed expiry an hour out caches a token that may already be dead; an assumed expiry of
    now mints on every single request. A body this shape means GitHub changed something, and that
    is worth a loud failure rather than a quiet limp.
    """
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise GitHubAuthError("GitHub returned a non-JSON installation token") from exc

    token = payload.get("token") if is_json_object(payload) else None
    expires_at = parse_timestamp(payload.get("expires_at")) if is_json_object(payload) else None
    if not isinstance(token, str) or not token or expires_at is None:
        raise GitHubAuthError("GitHub returned an installation token with no token or no expiry")
    return token, expires_at
