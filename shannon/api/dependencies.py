from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Depends, Request

from shannon.config import Settings
from shannon.domain.json import JsonObject
from shannon.github.webhooks.events import WebhookOutcome
from shannon.services.delivery.queue import DeliveryInbox


def get_settings_dep(request: Request) -> Settings:
    # Starlette types `app.state` as `Any`; the declared local stops that spreading to callers.
    found: Settings = request.app.state.settings
    return found


class EventIntake(Protocol):
    """What this route needs of the router, which is a decision and a fallback.

    `register` and `handles` are absent: a request handler must not change routes while running.
    """

    def will_act_on(self, event: str, action: str | None) -> bool: ...

    async def dispatch(
        self, event: str, action: str | None, payload: JsonObject
    ) -> WebhookOutcome: ...


def get_event_router(request: Request) -> EventIntake:
    found: EventIntake = request.app.state.event_router
    return found


def get_delivery_queue(request: Request) -> DeliveryInbox | None:
    found: DeliveryInbox | None = request.app.state.delivery_queue
    return found


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EventRouterDep = Annotated[EventIntake, Depends(get_event_router)]
DeliveryQueueDep = Annotated[DeliveryInbox | None, Depends(get_delivery_queue)]
