from __future__ import annotations

import logging
from typing import Protocol

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class Liveness(Protocol):
    """What the process can say about itself from the outside."""

    async def database_reachable(self) -> bool: ...

    def worker_running(self) -> bool: ...

    def bot_connected(self) -> bool: ...

    def poller_running(self) -> bool: ...

    def flusher_running(self) -> bool: ...


class HealthResponse(BaseModel):
    healthy: bool
    database: bool
    worker: bool
    bot: bool
    poller: bool
    flusher: bool
    # Which commit is answering: a change that was merged and never pulled reads from outside
    # exactly like a change that does not work.
    version: str


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, response: Response) -> HealthResponse:
    """Whether this process is doing its job, not merely listening.

    A stopped worker or an unreachable database leaves the webhook route accepting deliveries and
    answering 200 to every one of them, with nothing behind it that will ever act on them.
    """
    # No fallback, because `create_app` sets it unconditionally and a default would be a
    # branch nothing can reach.
    build: str = request.app.state.settings.build

    liveness: Liveness | None = getattr(request.app.state, "liveness", None)
    if liveness is None:
        # Nothing was wired in, which is how the route-level tests run: listening is all that
        # can honestly be claimed.
        return HealthResponse(
            healthy=True,
            database=True,
            worker=True,
            bot=True,
            poller=True,
            flusher=True,
            version=build,
        )

    database = await liveness.database_reachable()
    worker = liveness.worker_running()
    bot = liveness.bot_connected()
    poller = liveness.poller_running()
    flusher = liveness.flusher_running()

    # The board is reported without being counted: webhooks still arrive and threads are still
    # written without it, so failing the check would have an orchestrator restart a working
    # process. The poller is the only task with nothing wired to halt the process when it dies.
    healthy = database and worker and bot

    if not healthy:
        logger.warning("reporting unhealthy: database=%s worker=%s bot=%s", database, worker, bot)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not poller:
        logger.warning("the board is no longer being read, though everything else is working")

    # Its own line, so the log says which of the two uncounted tasks went.
    if healthy and not flusher:
        logger.warning(
            "captured conversations are no longer being published, though everything else "
            "is working"
        )

    return HealthResponse(
        healthy=healthy,
        database=database,
        worker=worker,
        bot=bot,
        poller=poller,
        flusher=flusher,
        version=build,
    )
