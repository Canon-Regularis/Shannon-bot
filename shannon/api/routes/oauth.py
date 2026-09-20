"""Where GitHub sends somebody back after they have proved who they are.

A public route reached by a browser rather than by GitHub's servers, carrying no signature and no
credential of its own: the `state` in the query string is the entire proof that this callback
belongs to the person who ran `/unregister` a moment ago. The permission check and the unbinding
happen when they run the command again, where there is somebody to report the answer to.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from shannon.services.verification import GitHubIdentityVerification, VerificationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])


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
            detail="That link is incomplete. Run /unregister in Discord again.",
        )

    try:
        verified = await verification.redeem(state=state, code=code)
    except VerificationError as refusal:
        # 400 rather than 401: nothing was authenticated here in the first place, and the caller
        # is a person looking at a page rather than a client that could retry with credentials.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=refusal.message
        ) from refusal

    return PlainTextResponse(
        f"Signed in as {verified.login}.\n\nGo back to Discord and run /unregister again to finish."
    )
