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
from shannon.services.verification import GitHubIdentityVerification, VerificationError

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
}


@router.get("/github/callback", response_class=PlainTextResponse)
async def github_callback(request: Request, code: str = "", state: str = "") -> PlainTextResponse:
    """Finish the round trip, in plain text.

    Plain text rather than HTML: no template engine, and so nowhere for somebody's login to be
    rendered unescaped. Neither `state` nor `code` is echoed back or logged, because this page is
    on the open internet and its logs are the one place a credential could come to rest.
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

    return PlainTextResponse(FINISHED[verified.purpose].format(login=verified.login))
