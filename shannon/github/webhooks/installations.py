"""Reading a delivery that says the App was installed, removed, paused or resumed.

The only events this bot handles that are not about an item: they change what it can see at all.
What they maintain is the account-to-installation map, kept by `services.installations`, and that
map is a cache with GitHub behind it, so a missed delivery costs a lookup rather than a broken
mirror.
"""

from __future__ import annotations

from dataclasses import dataclass

from shannon.domain.json import is_json_object


@dataclass(frozen=True, slots=True)
class InstallationEvent:
    """What one installation delivery says, once the fields that matter are checked."""

    installation_id: int
    account_login: str
    account_id: int | None


def parse_installation_event(payload: object) -> InstallationEvent | None:
    """The installation out of a delivery's `installation` block, or None with no usable one.

    Read from the block rather than from one event, so `installation` and
    `installation_repositories` share it. The account id is optional: a row recording a login with
    no id is still useful.
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
