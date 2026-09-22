"""Turning "which account is this call about" into a token that can see it.

The second half of App authentication: `app_auth` signs the JWT that proves this process is the
App, and this trades that JWT for a token scoped to one installation, kept until it expires.
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
from shannon.domain.json import is_json_object
from shannon.github.app_auth import app_jwt
from shannon.github.errors import GitHubAuthError
from shannon.github.mapping import parse_timestamp
from shannon.github.responses import json_object

logger = logging.getLogger(__name__)

# How long before a token expires it is thrown away and minted again. GitHub gives an hour, and
# five minutes covers a slow request, a retry behind it and a clock that disagrees.
REFRESH_MARGIN = timedelta(minutes=5)


class ResolvesInstallations(Protocol):
    """Which installation covers a GitHub account, or None if none does."""

    async def installation_for(self, owner: str) -> int | None: ...


class InstallationDirectory:
    """Owner to installation, out of the database.

    No in-process cache: the token cache in front absorbs the hot path, leaving one query an hour.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def installation_for(self, owner: str) -> int | None:
        async with self._sessionmaker() as session:
            found = await InstallationStore(session).for_owner(owner)
        if found is None:
            return None
        if found.suspended:
            # Suspended is not uninstalled, and minting against a suspended installation
            # fails, so there is nothing to be gained by trying.
            logger.info("the installation for %s is suspended, so nothing can be read", owner)
            return None
        return found.installation_id


class InstallationTokens:
    """Mints installation tokens and keeps each one until it is nearly stale.

    One lock per installation, so a burst for one repository mints once without blocking another.
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
        # None until asked for, "" once asked and not answered: the second stops a failing
        # read being retried on every `/register`.
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
            # Checked again inside the lock: without it, ten deliveries arriving together
            # each mint a token, and every mint counts against the App's rate limit.
            held = self._minted.get(installation)
            if held is not None and held[1] - REFRESH_MARGIN > self._now():
                return held[0]
            return await self._mint(installation, owner)

    async def installed_on(self, owner: str, name: str) -> int | None:
        """Which installation covers one repository, asked of GitHub rather than of the cache.

        A repository nobody has registered has no row, and no row means ask GitHub rather than
        not installed. A JWT, because there is no token until this has answered. GitHub 404s both
        for a repository that does not exist and for one the App was never installed on, and
        separating the two would say whether a private repository exists.
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

        payload = json_object(response)
        installation = payload.get("id")
        return installation if isinstance(installation, int) else None

    async def app_slug(self) -> str:
        """The App's own slug, for building the "install me" link, read once and kept.

        Read from GitHub rather than configured: it never changes for a given App.
        """
        if self._slug is not None:
            return self._slug
        if not self.app_token():
            return ""

        response = await self._http.get(
            "/app", headers={"Authorization": f"Bearer {self.app_token()}"}
        )
        if response.status_code >= 400:
            # Not raised: the slug only makes a message more helpful, and `/register` should
            # not fail because the link could not be prettified.
            logger.warning("could not read the app's own slug (%s)", response.status_code)
            return ""

        slug = json_object(response).get("slug")
        self._slug = slug if isinstance(slug, str) else ""
        return self._slug

    async def _mint(self, installation: int, owner: str) -> str:
        """Ask GitHub for a token, or answer "" for an installation that has gone.

        A 404 means the installation was removed between the directory read and this call, which
        is permanent and the same outcome as never having been installed. Everything else raises:
        a 401 is a key GitHub will not accept, and calling that "no installation" misdirects.
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


def _minted(response: httpx.Response) -> tuple[str, datetime]:
    """The token and its expiry, refusing a body that is missing either.

    Neither default is safe: an assumed hour caches a token that may already be dead, and an
    assumed expiry of now mints on every request. A body this shape means GitHub changed something.
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
