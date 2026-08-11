from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from shannon.api.dependencies import DeliveryGuardDep, EventRouterDep, SettingsDep
from shannon.github.webhooks.events import EventRouter, WebhookOutcome
from shannon.github.webhooks.signature import SignatureResult, verify
from shannon.services.idempotency import DeliveryGuard, DeliveryStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookResponse(BaseModel):
    status: WebhookOutcome
    event: str
    action: str | None = None


@router.post("/github", response_model=WebhookResponse)
async def receive_github_webhook(
    request: Request,
    event_router: EventRouterDep,
    settings: SettingsDep,
    delivery_guard: DeliveryGuardDep,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> WebhookResponse:
    if not x_github_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="X-GitHub-Event header is missing"
        )
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="X-GitHub-Delivery header is missing"
        )

    body = await request.body()
    _require_valid_signature(
        body, settings.github_webhook_secret.get_secret_value(), x_hub_signature_256
    )
    payload = _decode(body)
    action = payload.get("action")
    if action is not None and not isinstance(action, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Payload action must be a string"
        )

    outcome = await _dispatch_once(
        delivery_guard,
        event_router,
        event=x_github_event,
        delivery_id=x_github_delivery,
        action=action,
        payload=payload,
        body=body,
    )
    logger.info(
        "webhook %s.%s delivery=%s outcome=%s", x_github_event, action, x_github_delivery, outcome
    )
    return WebhookResponse(status=outcome, event=x_github_event, action=action)


async def _dispatch_once(
    delivery_guard: DeliveryGuard | None,
    event_router: EventRouter,
    *,
    event: str,
    delivery_id: str,
    action: str | None,
    payload: dict[str, Any],
    body: bytes,
) -> WebhookOutcome:
    # Nothing to protect against a repeat of an event we would ignore anyway, and recording one
    # would grow the delivery table for no reason.
    if delivery_guard is None or not event_router.will_act_on(event, action):
        return await event_router.dispatch(event, action, payload)

    if not await delivery_guard.claim(delivery_id, event, body):
        return WebhookOutcome.DUPLICATE

    try:
        outcome = await event_router.dispatch(event, action, payload)
    except Exception:
        # Drop the claim so GitHub's retry is treated as fresh work rather than a duplicate.
        await delivery_guard.release(delivery_id)
        logger.exception("handler failed for delivery %s", delivery_id)
        raise

    await delivery_guard.complete(
        delivery_id,
        DeliveryStatus.PROCESSED if outcome is WebhookOutcome.PROCESSED else DeliveryStatus.IGNORED,
    )
    return outcome


_SIGNATURE_FAILURES = {
    SignatureResult.MISSING: "X-Hub-Signature-256 header is missing",
    SignatureResult.MALFORMED: "X-Hub-Signature-256 header is malformed",
    SignatureResult.INVALID: "Signature does not match the request body",
}


def _require_valid_signature(body: bytes, secret: str, header_value: str | None) -> None:
    # Fail closed. An unconfigured secret would otherwise let anyone post events.
    if not secret:
        logger.error("SHANNON_GITHUB_WEBHOOK_SECRET is not set, rejecting delivery")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret is not configured",
        )

    result = verify(body, secret, header_value)
    if result is SignatureResult.VALID:
        return

    logger.warning("rejecting webhook delivery: %s", result)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=_SIGNATURE_FAILURES[result]
    )


def _decode(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not valid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object"
        )
    return payload
