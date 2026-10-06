"""The authorisations people grant so a server can reach their project board.

Issue #170. This is the only place in the project that keeps a credential belonging to a person,
and the only column in the schema that is encrypted. Both of those are deliberate and neither is
something to extend casually, so the reasoning is written down here.

**Why a board keeps a token when `/link` does not.** The identity round trip is finished by the
answer: GitHub says who somebody is, the row is written, and the token is dropped on the way out.
A board is read every couple of seconds for as long as it is linked, with nobody at a keyboard, so
the authorisation has to outlive the browser visit that granted it. There is no version of polling
that does not keep something.

**Why it is encrypted.** The rest of what this project stores about a person is a fact ABOUT them -
a login, an account id, a preference not to be pinged. Losing those is bad. This is a thing that
ACTS as them: with it, GitHub will move cards and read boards as that account. So it is encrypted
with a key held in the environment, which is what makes a stolen copy of the table worth nothing by
itself.

**Why it fails open on the board and closed on the credential.** A missing or unusable key means
this answers "no authorisation", which reads downstream as a board that cannot be read - loudly,
once per board, through the same sentence an unopenable board has always used. It does NOT stop the
process starting. A second lock added over a working system must not become the reason the system
stops: webhooks, deliveries and every other server would go down over a board setting, which is a
far worse failure than the one it was guarding against.

**Why a row that will not decrypt reads as absent.** A key rotated away, a row written by another
deployment, a corrupted column - none of them is a credential, and treating them as anything other
than "there isn't one" would mean sending a bearer token GitHub never issued, or worse, guessing.
The person re-authorises and the row is replaced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.board_authorizations import (
    BoardAuthorizationStore,
    HeldAuthorization,
)
from shannon.db.stores.repositories import RepositoryStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Granted:
    """An authorisation, as the thing that can act with it plus who it belongs to."""

    token: str
    github_login: str
    github_user_id: int


class BoardCredentials:
    """Keeping and handing back the authorisations a board is read and written under.

    One object for both halves on purpose. The encryption and the lookup are the same concern -
    a credential nobody can decrypt is a credential nobody has - and splitting them would let a
    caller fetch a row and forget to ask whether it could be read.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        keys: str = "",
    ) -> None:
        self._sessionmaker = sessionmaker
        self._cipher = _cipher(keys)

    @property
    def usable(self) -> bool:
        """Whether this deployment can keep an authorisation at all.

        Asked before a command offers the round trip, so that somebody is told what is missing
        rather than sent to GitHub to grant something that cannot then be stored.
        """
        return self._cipher is not None

    async def remember(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        github_login: str,
        github_user_id: int,
        token: str,
    ) -> bool:
        """Keep one person's authorisation for one server, answering whether it was kept.

        False where there is no key. The caller is the OAuth callback, which has just sent
        somebody through a browser, so it has to be able to say that the trip was wasted rather
        than report a success that stored nothing.
        """
        if self._cipher is None:
            logger.error(
                "discord:%s authorised a board in guild %s and it could not be kept, because "
                "SHANNON_BOARD_CREDENTIAL_KEY is not set or could not be read",
                discord_user_id,
                guild_id,
            )
            return False

        async with self._sessionmaker() as session, session.begin():
            await BoardAuthorizationStore(session).remember(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                github_login=github_login,
                github_user_id=github_user_id,
                secret=self._cipher.encrypt(token.encode("utf-8")).decode("ascii"),
            )
        return True

    async def granted_to(self, *, guild_id: int, discord_user_id: int) -> Granted | None:
        """The authorisation this member granted in this server, if it is still readable."""
        async with self._sessionmaker() as session:
            held = await BoardAuthorizationStore(session).held(
                guild_id=guild_id, discord_user_id=discord_user_id
            )
        return self._opened(held, guild_id=guild_id, discord_user_id=discord_user_id)

    async def moving(self, *, guild_id: int, discord_user_id: int) -> str:
        """The credential a card is moved with, which is the mover's own, or an empty string.

        Satisfies `WhoIsMovingTheCard`. Deliberately NOT a fallback to whoever linked the board:
        moving a card as somebody else is the thing issue #170 exists to stop, so an empty answer
        is a refusal and the caller turns it into one before anything is written.

        Separate from `reading` above because they answer different questions. A board's own reads
        belong to the board and happen with nobody present; a write belongs to whoever asked for
        it. One method taking a nullable member would make the difference a runtime detail.
        """
        granted = await self.granted_to(guild_id=guild_id, discord_user_id=discord_user_id)
        return granted.token if granted else ""

    async def reading(self, owner: str, project_number: int) -> str:
        """The credential a linked board's own reads are made under, or an empty string.

        Satisfies `WhoTheBoardIsReadAs`, and the empty string is the contract rather than a
        shrug: no member's credential goes out, and nothing else does either.
        `HttpProjectBoards._read_as` refuses before any request with a GitHubAuthError, which is
        in the `GitHubNotFoundError | GitHubAuthError` pair every unreadable-board site already
        catches - so "nobody authorised this" needs no new branch anywhere: it is a board that
        will not open, which is what it is.

        Keyed on the BOARD and resolved through the member recorded against it, never on the
        owner. Two servers may link two different boards owned by the same account, and an
        owner-keyed lookup would read one server's board under the other's member's grant.

        And the board is resolved by whose it actually is, in one query - see
        `RepositoryStore.mirroring`. This used to look for the owner as stored and then fall back
        to ANY row with the number and a null owner, which is any server's own board of that
        number: a server that named another's board by its owner was found first, and its
        member's credential read the other server's board. Issue #201.
        """
        async with self._sessionmaker() as session:
            mirroring = await RepositoryStore(session).mirroring(
                project_number=project_number, owner=owner
            )
            # Exactly one, or nobody's. Two repositories on one board could be linked before the
            # owner was resolved, and choosing between them would be reading one server's board
            # under the other server's member - the thing keying this on the board prevents. So
            # neither is read until one lets go, and the poll says the board will not open.
            linked = mirroring[0] if len(mirroring) == 1 else None
            if linked is None or linked.project_linked_by is None:
                return ""
            held = await BoardAuthorizationStore(session).held(
                guild_id=linked.discord_guild_id, discord_user_id=linked.project_linked_by
            )
        granted = self._opened(
            held,
            guild_id=linked.discord_guild_id,
            discord_user_id=linked.project_linked_by,
        )
        return granted.token if granted else ""

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool:
        """Drop one authorisation, answering whether there was one to drop.

        Deleting this copy is not a revocation on GitHub's side, and the reply that reports this
        has to say so: the grant stands until the person withdraws it in their own settings.
        """
        async with self._sessionmaker() as session, session.begin():
            return await BoardAuthorizationStore(session).forget(
                guild_id=guild_id, discord_user_id=discord_user_id
            )

    def _opened(
        self, held: HeldAuthorization | None, *, guild_id: int, discord_user_id: int
    ) -> Granted | None:
        """Decrypt a row, or read it as absent and say why once.

        `self._cipher is None` and `held is None` are one condition rather than two arms: both
        mean there is nothing to hand back, and nothing downstream can act differently on which.
        """
        if self._cipher is None or held is None:
            return None

        try:
            token = self._cipher.decrypt(held.secret.encode("ascii")).decode("utf-8")
        except InvalidToken:
            # Never the ciphertext itself, and never the key. A row this deployment cannot read is
            # one written under a key it no longer has, which is a thing an operator fixes by
            # asking the person to authorise again rather than by inspecting bytes.
            logger.warning(
                "the board authorisation discord:%s granted in guild %s cannot be read with the "
                "current SHANNON_BOARD_CREDENTIAL_KEY, so it counts as absent; that person can "
                "authorise again to replace it",
                discord_user_id,
                guild_id,
            )
            return None

        return Granted(
            token=token, github_login=held.github_login, github_user_id=held.github_user_id
        )


def _cipher(keys: str) -> MultiFernet | None:
    """The cipher for a comma-separated key list, or None where there is nothing usable.

    `MultiFernet` rather than `Fernet`, for one reason worth four extra lines: it encrypts with the
    first key and decrypts with any of them. So rotating a key is "put the new one in front, deploy,
    take the old one out next time" instead of "every person who authorised a board does it again".

    None rather than a raise. This is built at startup beside everything else, and a malformed key
    must not be the reason a bot stops answering webhooks - see the module docstring.
    """
    usable: list[Fernet] = []
    for key in (one.strip() for one in keys.split(",")):
        if not key:
            continue
        try:
            usable.append(Fernet(key))
        except (ValueError, TypeError):
            # Said once, at startup, naming the setting rather than the key. A Fernet key is
            # thirty-two url-safe base64 bytes, and the commonest way to get here is pasting one
            # with the quotes still on it.
            logger.error(
                "a key in SHANNON_BOARD_CREDENTIAL_KEY is not a Fernet key, so it was ignored; "
                'generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

    return MultiFernet(usable) if usable else None
