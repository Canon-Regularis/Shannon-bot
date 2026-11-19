"""A stand-in for the verification service, as the OAuth routes see it (`RunsTheRoundTrip`).

Moved here from the route tests by #266, so `IMPLEMENTATIONS` holds it against the protocol the
routes declare: a stand-in nothing checks is one that can drift away from what it replaces.
"""

from __future__ import annotations

from shannon.domain.enums import VerificationPurpose
from shannon.services.verification import VerificationError, Verified

DISCORD = "https://discord.com/oauth2/authorize?state=sealed"
GITHUB = "https://github.com/login/oauth/authorize?state=sealed-again"


class FakeVerification:
    """What the routes ask of the service, and what each stop handed it."""

    # On the class as well as the instance, because the conformance check looks a member up on the
    # class: the real service answers it with a property.
    can_sign_in_with_discord: bool = True

    def __init__(self, *, can: bool = True, refusal: str | None = None) -> None:
        self.can_sign_in_with_discord = can
        self.refusal = refusal
        self.started: list[tuple[str, str]] = []
        self.proved: list[tuple[str, str, str]] = []
        self.redeemed: list[tuple[str, str, str]] = []

    def refuse_if_told_to(self) -> None:
        if self.refusal is not None:
            raise VerificationError(self.refusal)

    async def start(self, *, state: str, browser: str) -> str:
        self.started.append((state, browser))
        self.refuse_if_told_to()
        return DISCORD

    async def prove_on_discord(self, *, state: str, code: str, browser: str) -> str:
        self.proved.append((state, code, browser))
        self.refuse_if_told_to()
        return GITHUB

    async def redeem(self, *, state: str, code: str, browser: str) -> Verified:
        self.redeemed.append((state, code, browser))
        self.refuse_if_told_to()
        return Verified(
            guild_id=1,
            discord_user_id=555,
            login="octocat",
            github_user_id=583231,
            purpose=VerificationPurpose.LINK,
        )
