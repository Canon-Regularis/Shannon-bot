from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from shannon.api.dependencies import (
    DeliveryQueueDep,
    EventIntake,
    EventRouterDep,
    SettingsDep,
)
from shannon.domain.json import JsonObject, is_json_object
from shannon.github.webhooks.events import WebhookOutcome
from shannon.github.webhooks.signature import SignatureResult, verify_any
from shannon.services.delivery.queue import DeliveryInbox

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
    queue: DeliveryQueueDep,
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

    body = await _read_within_limit(request)
    _require_valid_signature(
        body,
        (
            settings.github_app_webhook_secret.get_secret_value(),
            settings.github_webhook_secret.get_secret_value(),
        ),
        x_hub_signature_256,
    )
    payload = _decode(body)
    action = payload.get("action")
    if action is not None and not isinstance(action, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Payload action must be a string"
        )

    outcome = await _accept(
        queue,
        event_router,
        event=x_github_event,
        delivery_id=x_github_delivery,
        action=action,
        payload=payload,
    )
    logger.info(
        "webhook %s.%s delivery=%s outcome=%s", x_github_event, action, x_github_delivery, outcome
    )
    return WebhookResponse(status=outcome, event=x_github_event, action=action)


async def _accept(
    queue: DeliveryInbox | None,
    event_router: EventIntake,
    *,
    event: str,
    delivery_id: str,
    action: str | None,
    payload: JsonObject,
) -> WebhookOutcome:
    """Write the delivery down and answer. The work happens in the worker.

    GitHub gives an endpoint ten seconds and never redelivers a delivery it recorded as failed,
    so anything slow done here risks losing the event outright. Nothing below this line talks
    to Discord.
    """
    # Nothing to protect against a repeat of an event we would ignore anyway, and recording one
    # would grow the queue for no reason.
    if not event_router.will_act_on(event, action):
        return WebhookOutcome.IGNORED

    # Without a queue the route has nowhere to put the work, so it does it inline. That is how
    # the route-level tests run, with no database behind them.
    if queue is None:
        return await event_router.dispatch(event, action, payload)

    if not await queue.enqueue(delivery_id, event, payload):
        return WebhookOutcome.DUPLICATE
    return WebhookOutcome.ACCEPTED


# GitHub will not send a payload larger than this, and says so. The endpoint is open to the
# internet and the body is read into memory before anything can be checked, because the
# signature covers the whole of it, so the limit has to be applied during the read.
MAX_BODY_BYTES = 25 * 1024 * 1024


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail="Body is larger than GitHub will ever send",
    )


async def _read_within_limit(request: Request) -> bytes:
    """Read the body, giving up once it goes past what GitHub would ever send.

    The running count is the real limit; Content-Length is only a free early exit. Nothing
    obliges a client to send that header, and the signature covers the body, so an anonymous
    caller can stream a chunked request of any size before anything can be verified.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        logger.warning("rejecting a delivery declaring %s bytes", declared)
        raise _too_large()

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            logger.warning("rejecting a delivery still arriving past %s bytes", MAX_BODY_BYTES)
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


_SIGNATURE_FAILURES = {
    SignatureResult.MISSING: "X-Hub-Signature-256 header is missing",
    SignatureResult.MALFORMED: "X-Hub-Signature-256 header is malformed",
    SignatureResult.INVALID: "Signature does not match the request body",
}


def _require_valid_signature(body: bytes, secrets: Sequence[str], header_value: str | None) -> None:
    """Accept a delivery signed with any secret this deployment knows.

    Two rather than one, for the window in which a repository webhook configured by hand and the
    GitHub App's own webhook are both live. Without it the changeover is a flag day: delete the
    old webhook a moment early and deliveries are refused, a moment late and they arrive twice.

    That second case is the one to get out of quickly, and nothing here can detect it: GitHub
    gives the two copies different delivery ids, so the queue's own duplicate check cannot see
    them and every commit line is posted twice. Delete the repository webhook as soon as the App
    is installed.
    """
    result = verify_any(body, secrets, header_value)
    if result is SignatureResult.VALID:
        return
    # Fail closed. An unconfigured secret would otherwise let anyone post events.
    if not any(secrets):
        logger.error(
            "neither SHANNON_GITHUB_APP_WEBHOOK_SECRET nor SHANNON_GITHUB_WEBHOOK_SECRET is "
            "set, rejecting delivery"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret is not configured",
        )

    logger.warning("rejecting webhook delivery: %s", result)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=_SIGNATURE_FAILURES[result]
    )


def _decode(body: bytes) -> JsonObject:
    """The body as something checked, which is what `domain.json` exists to hand back.

    `is_json_object` rather than `isinstance(payload, dict)`: for what `json.loads` produces the
    two accept the same values, but only the guard carries the key type onwards, and carrying it
    is the whole point of narrowing here rather than further down.
    """
    try:
        payload: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not valid JSON"
        ) from exc

    if not is_json_object(payload):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object"
        )
    return payload
