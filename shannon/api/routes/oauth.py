"""Where GitHub sends somebody back after they have proved who they are.

A public route reached by a browser rather than by GitHub's servers, carrying no signature and no
credential of its own: the `state` in the query string is the entire proof that this callback
belongs to the person who ran the command a moment ago. What happens next depends on which command
that was, which the row records and this page reads.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from shannon.domain.enums import VerificationPurpose
from shannon.services.boards import said
from shannon.services.verification import (
    BoardLinked,
    BoardNotLinked,
    GitHubIdentityVerification,
    VerificationError,
    Verified,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

# What the browser is told, by what the link was for. Until issue #144 the row carried no such
# thing, so this page could only say "run the command again" and leave which one to the reader:
# naming the wrong one sends somebody to a command they are not allowed to run.
#
# A mapping rather than a branch, because it has to be total and a branch cannot be made to show
# that it is. A purpose with no page here is a KeyError in front of somebody who has just signed
# in, so a test holds the two sets against each other instead.
FINISHED: Final[dict[VerificationPurpose, str]] = {
    VerificationPurpose.LINK: (
        "Signed in as {login}.\n\nThat server has your GitHub account on record now. "
        "There is nothing else to run."
    ),
    VerificationPurpose.REGISTER: (
        "Signed in as {login}.\n\nGo back to Discord and run /register again, with the same "
        "repository link, to finish."
    ),
    VerificationPurpose.UNREGISTER: (
        "Signed in as {login}.\n\nGo back to Discord and run /unregister again to finish."
    ),
    # A board authorisation with no board riding on it: /board authorise. It used to say that a
    # board could now be read and that /set_board took it back - neither of which was true of
    # somebody who had only authorised, and the second of which was never true at all.
    VerificationPurpose.BOARD: (
        "Signed in as {login}.\n\nYour authorisation is kept, so a card this bot moves for you "
        "from Discord moves as you on GitHub. There is nothing else to run.\n\nYou can take this "
        "back at any time: /board withdraw in Discord, and Applications in your GitHub settings."
    ),
}

# A board link that carried a board, and linked it. Issue #201: following one link is the whole of
# linking a board now, so this is the last thing anybody is told about it - which board, under
# whose account, and how to undo both halves.
LINKED: Final = (
    "Signed in as {login}.\n\n{said}\n\nThere is nothing else to run. /board unlink in Discord "
    "stops the mirroring, and Applications in your GitHub settings takes back the authorisation."
)

# A board link that carried a board and could not link it. The authorisation is kept - it is what
# a card is moved with and what the next /board link opens the board with - so the page says so,
# and why the board was not linked, rather than a failure for a sign-in that worked.
NOT_LINKED: Final = (
    "Signed in as {login}.\n\nYour authorisation is kept, but the board was not linked: "
    "{reason}\n\nOnce that is fixed, run /board link in Discord again."
)

# The reason when the reason is nothing a person can act on. The log has the rest.
UNLINKABLE: Final = (
    "something went wrong on this bot's side, and nothing about the board changed. Trying again "
    "in a minute is the right thing to do."
)


@router.get("/github/callback", response_class=PlainTextResponse)
async def github_callback(request: Request, code: str = "", state: str = "") -> PlainTextResponse:
    """Finish the round trip, in plain text.

    Plain text rather than HTML: no template engine, and so nowhere for somebody's login - or a
    board's title, which is anybody's free text - to be rendered unescaped. Neither `state` nor
    `code` is echoed back or logged, because this page is on the open internet and its logs are
    the one place a credential could come to rest.
    """
    verification: GitHubIdentityVerification | None = getattr(
        request.app.state, "verification", None
    )
    if verification is None or not verification.configured:
        logger.error("an oauth callback arrived but identity verification is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Identity verification is not configured on this deployment.",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That link is incomplete. Run the command in Discord again.",
        )

    try:
        verified = await verification.redeem(state=state, code=code)
    except VerificationError as refusal:
        # 400 rather than 401: nothing was authenticated here in the first place, and the caller
        # is a person looking at a page rather than a client that could retry with credentials.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=refusal.message
        ) from refusal

    return PlainTextResponse(_page_for(verified))


def _page_for(verified: Verified) -> str:
    """The page for a finished round trip.

    The board's title and the reason are handed to `format` as VALUES rather than joined into the
    template first. A title is somebody's free text, and a brace in it read as a placeholder would
    fail this page after the board had already linked.
    """
    if isinstance(verified.board, BoardLinked):
        return LINKED.format(login=verified.login, said=said(verified.board.link))
    if isinstance(verified.board, BoardNotLinked):
        return NOT_LINKED.format(login=verified.login, reason=verified.board.reason or UNLINKABLE)
    return FINISHED[verified.purpose].format(login=verified.login)
