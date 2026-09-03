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


class HealthResponse(BaseModel):
    healthy: bool
    database: bool
    worker: bool
    bot: bool
    poller: bool


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, response: Response) -> HealthResponse:
    """Whether this process is doing its job, not merely listening.

    The endpoint answering at all only proves uvicorn is up. Everything that matters happens
    behind it: a worker that has stopped, or a database that cannot be reached, leaves the
    webhook route accepting deliveries that nothing will ever act on, and answering 200 to
    every one of them. Both are reported here so an orchestrator can restart the process
    instead of leaving it to look healthy while the queue grows.
    """
    liveness: Liveness | None = getattr(request.app.state, "liveness", None)
    if liveness is None:
        # Nothing was wired in, which is how the route-level tests run. Listening is all that
        # can honestly be claimed.
        return HealthResponse(healthy=True, database=True, worker=True, bot=True, poller=True)

    database = await liveness.database_reachable()
    worker = liveness.worker_running()
    bot = liveness.bot_connected()
    poller = liveness.poller_running()

    # The board is the one thing reported without being counted. This process is still doing its
    # job without it: webhooks arrive, threads are written, and only board movement stops. Failing
    # the check would have an orchestrator restart a working process and throw away whatever the
    # worker had in hand. Saying nothing at all is the other mistake, and the one this is here to
    # stop: the poller is the only task with nothing wired to halt the process when it dies, so it
    # goes with a line in the log and everything after that answers that all is well.
    healthy = database and worker and bot

    if not healthy:
        logger.warning("reporting unhealthy: database=%s worker=%s bot=%s", database, worker, bot)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not poller:
        logger.warning("the board is no longer being read, though everything else is working")

    return HealthResponse(healthy=healthy, database=database, worker=worker, bot=bot, poller=poller)
