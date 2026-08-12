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


class HealthResponse(BaseModel):
    healthy: bool
    database: bool
    worker: bool


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
        return HealthResponse(healthy=True, database=True, worker=True)

    database = await liveness.database_reachable()
    worker = liveness.worker_running()
    healthy = database and worker

    if not healthy:
        logger.warning("reporting unhealthy: database=%s worker=%s", database, worker)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(healthy=healthy, database=database, worker=worker)
