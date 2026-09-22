"""Which GitHub App installation covers which account.

Read on the way to every GitHub call, so the request carries a token scoped to the account it is
about. A cache rather than a source of truth: GitHub is authoritative and can always be asked, so
a missing or stale row costs one request and no reconciliation job stands behind this table.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import rows_changed
from shannon.db.models import GitHubInstallation

# The first key of the per-account advisory lock. Postgres keeps two-integer keys apart
# from single-bigint ones, and `UserLinkStore` uses the latter; the other two-integer
# space in this project leads with `_ONE_ITEM_AT_A_TIME`, so all three stay apart.
_ONE_ACCOUNT_AT_A_TIME = 8_532


class InstallationStore:
    """The account-to-installation map, as this bot currently understands it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_owner(self, account_login: str) -> GitHubInstallation | None:
        """The installation covering an account, or None if none is known here.

        None means "ask GitHub", not "not installed": treating an empty cache as a refusal
        would make a missed webhook look exactly like an App nobody ever installed.
        """
        found: GitHubInstallation | None = await self._session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.account_login == account_login.strip().lower()
            )
        )
        return found

    async def remember(
        self,
        *,
        installation_id: int,
        account_login: str,
        account_id: int | None = None,
        suspended: bool = False,
    ) -> None:
        """Write down what GitHub just said, replacing whatever was there.

        Two unique constraints reach this row and only one can be named in an `ON CONFLICT`, so
        the other is cleared first: a reinstall keeps the login and issues a NEW installation id,
        and a transfer keeps the id and changes the login. `account_id` is only overwritten when
        this caller has one, since it is the only thing that tells a rename apart from somebody
        taking a freed name.

        Two steps against two constraints cannot be made atomic by `ON CONFLICT`, which
        names one of them, and a retry loop only has the retries colliding instead. So an
        advisory lock per account, exactly as `UserLinkStore.link` takes one per guild and
        for the same reason. Keyed on the login rather than the installation id, because
        the login is what two writers contend for: a reinstall keeps it and brings a new id.
        """
        login = account_login.strip().lower()
        await self._session.execute(
            select(func.pg_advisory_xact_lock(_ONE_ACCOUNT_AT_A_TIME, func.hashtext(login)))
        )
        await self._session.execute(
            delete(GitHubInstallation).where(
                GitHubInstallation.account_login == login,
                GitHubInstallation.installation_id != installation_id,
            )
        )

        values: dict[str, object] = {
            "installation_id": installation_id,
            "account_login": login,
            "account_id": account_id,
            "suspended": suspended,
        }
        # Declared because the conditional key below is an int, and the literal alone would fix
        # the value type at `str | bool`.
        settled: dict[str, object] = {"account_login": login, "suspended": suspended}
        if account_id is not None:
            settled["account_id"] = account_id

        await self._session.execute(
            pg_insert(GitHubInstallation)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_github_installations_installation_id", set_=settled
            )
        )

    async def forget(self, installation_id: int) -> bool:
        """Drop an installation somebody uninstalled, answering whether there was one.

        GitHub sends `installation.deleted` to every subscriber, including one that never held
        a row for it, so the answer decides whether anything is logged as removed.
        """
        changed = await rows_changed(
            self._session,
            delete(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id),
        )
        return bool(changed)
