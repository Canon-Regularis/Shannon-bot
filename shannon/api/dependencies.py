from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from shannon.config import Settings
from shannon.github.webhooks.events import EventRouter
from shannon.services.delivery_queue import DeliveryQueue


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_event_router(request: Request) -> EventRouter:
    return request.app.state.event_router


def get_delivery_queue(request: Request) -> DeliveryQueue | None:
    return request.app.state.delivery_queue


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EventRouterDep = Annotated[EventRouter, Depends(get_event_router)]
DeliveryQueueDep = Annotated[DeliveryQueue | None, Depends(get_delivery_queue)]
