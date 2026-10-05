"""The three stops a browser makes after somebody opens a one-time link.

Public routes reached by a browser rather than by GitHub's or Discord's servers, carrying no
signature of their own. Found reviewing #201: the `state` in the query string used to be the whole
of the proof, and a forwarded link signed its issuer in as whoever clicked it. So a round trip is
now held to one browser by a cookie, and to one member by Discord:

1. `/oauth/start` leaves the cookie and sends the browser to Discord. It writes nothing, so a link
   preview opening it costs nothing.
2. `/oauth/discord/callback` hears from Discord who is holding the browser, binds the browser to
   the link if that is the member it was issued for, and sends it on to GitHub.
3. `/oauth/github/callback` finishes, for that browser alone. What it says depends on which command
   the link came from, which the row records and this page reads.
"""

from __future__ import annotations

import logging
import secrets
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse

from shannon.domain.enums import VerificationPurpose
from shannon.services.boards import said
from shannon.services.verification import (
    BROWSER_BYTES,
    BROWSER_SHAPE,
    LINK_LIFETIME,
    BoardLinked,
    BoardNotLinked,
    GitHubIdentityVerification,
    VerificationError,
    Verified,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

# The cookie that holds a round trip to one browser. `__Host-` makes a browser refuse it unless it
# is Secure, names no Domain and has Path=/ - which is what stops a sibling subdomain, or anybody
# on plain http, planting a value of their own for a victim's browser to send here. Path=/ costs
# nothing: Caddy forwards only `/health`, `/webhooks/*` and `/oauth/*` to this app.
ROUND_TRIP_COOKIE: Final = "__Host-shannon_round_trip"
# As long as a link lives, and no longer.
ROUND_TRIP_SECONDS: Final = int(LINK_LIFETIME.total_seconds())

INCOMPLETE: Final = "That link is incomplete. Run the command in Discord again."

# Discord answers `error=access_denied` when somebody presses Cancel on its page.
CANCELLED: Final = (
    "You did not finish signing in to Discord, so nothing happened. Run the command in Discord "
    "again when you are ready."
)

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


def _verification(request: Request) -> GitHubIdentityVerification:
    """The verification service, where this deployment can run a round trip at all.

    All three stops ask the same thing. Without the Discord half no browser can ever be bound to
    the member a link was issued for, so nothing here could be finished - and failing closed is
    what was chosen. The commands refuse to hand a link out in that state, so arriving here anyway
    means a link from before the settings went, and a 500 says plainly it is the deployment's fault.
    """
    verification: GitHubIdentityVerification | None = getattr(
        request.app.state, "verification", None
    )
    if verification is None or not verification.can_sign_in_with_discord:
        logger.error("an oauth request arrived but this deployment cannot run the round trip")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Identity verification is not configured on this deployment.",
        )
    return verification


def _round_trip_cookie(request: Request) -> str:
    """The round-trip cookie this browser sent, read by its exact name, or nothing.

    Not `request.cookies`, which strips every cookie name with `str.strip()` - and that removes a
    leading no-break space or NEL as well as a blank. So a cookie the browser holds under this name
    behind one of those, a name no `__Host-` rule ever applied to and so one a sibling domain or a
    plain-http response could have planted, was read as this one, and `start` then handed it back
    as a genuine `__Host-` cookie. Found reviewing #201's fix. Only blanks and tabs are stripped
    here, the name must match exactly, and a name that arrives twice reads as nothing: a browser
    holds one `__Host-` cookie of a name, so a second is somebody else's.
    """
    found = [
        value.strip(" \t")
        for header in request.headers.getlist("cookie")
        for name, sep, value in (chunk.partition("=") for chunk in header.split(";"))
        if sep and name.strip(" \t") == ROUND_TRIP_COOKIE
    ]
    return found[0] if len(found) == 1 else ""


def _refused(refusal: VerificationError) -> HTTPException:
    """A refusal as the browser sees it.

    400 rather than 401: nothing was authenticated here in the first place, and the caller is a
    person looking at a page rather than a client that could retry with credentials.
    """
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=refusal.message)


@router.get("/start", response_class=RedirectResponse)
async def start(request: Request, state: str = "") -> RedirectResponse:
    """Hold the round trip to this browser, and send it to Discord.

    The cookie is set here and nowhere else. A well-formed one the browser already holds is kept
    rather than replaced, so two links followed in one browser do not knock each other over.
    Always Secure: the app sees plain http behind Caddy, so the scheme of this request says nothing
    about the one the browser used.
    """
    verification = _verification(request)
    if not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INCOMPLETE)

    held = _round_trip_cookie(request)
    browser = held if BROWSER_SHAPE.fullmatch(held) else secrets.token_urlsafe(BROWSER_BYTES)
    try:
        discord = await verification.start(state=state, browser=browser)
    except VerificationError as refusal:
        raise _refused(refusal) from refusal

    response = RedirectResponse(discord, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ROUND_TRIP_COOKIE,
        browser,
        max_age=ROUND_TRIP_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    # A redirect carrying a sealed state is the last thing a cache should keep.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/discord/callback", response_class=RedirectResponse)
async def discord_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> RedirectResponse:
    """Hear from Discord who is holding this browser, and send it on to GitHub if it is theirs.

    The cookie is passed on as it arrived, missing included. The service refuses anything that is
    not a well-formed browser before it makes a key out of it, so the shape is checked in one place
    rather than two.
    """
    verification = _verification(request)
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CANCELLED)
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INCOMPLETE)

    try:
        github = await verification.prove_on_discord(
            state=state, code=code, browser=_round_trip_cookie(request)
        )
    except VerificationError as refusal:
        raise _refused(refusal) from refusal

    response = RedirectResponse(github, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/github/callback", response_class=PlainTextResponse)
async def github_callback(request: Request, code: str = "", state: str = "") -> PlainTextResponse:
    """Finish the round trip, in plain text, for the browser that proved itself and no other.

    Plain text rather than HTML: no template engine, and so nowhere for somebody's login - or a
    board's title, which is anybody's free text - to be rendered unescaped. Neither `state` nor
    `code` is echoed back or logged, because this page is on the open internet and its logs are
    the one place a credential could come to rest.
    """
    verification = _verification(request)
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INCOMPLETE)

    try:
        verified = await verification.redeem(
            state=state, code=code, browser=_round_trip_cookie(request)
        )
    except VerificationError as refusal:
        raise _refused(refusal) from refusal

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
