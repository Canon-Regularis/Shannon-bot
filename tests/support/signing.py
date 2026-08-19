"""Putting a webhook on the wire the way GitHub does.

One module because both tiers need the same thing. What differs between them is depth, not the
request: the unit tier builds a bare route, the integration tier builds the whole container, and
nothing about signing a body changes between the two.
"""

from __future__ import annotations

import json
from typing import Any

from httpx import Response

from shannon.github.webhooks.signature import sign

SECRET = "test-webhook-secret"


async def post(
    client: Any,
    event: str,
    payload: Any = None,
    *,
    body: bytes | None = None,
    delivery: str = "delivery-1",
    signature: str | object | None = ...,
    secret: str = SECRET,
) -> Response:
    """Post a webhook, signing the body the way GitHub does unless told otherwise.

    `client` is anything with an async `post`, which covers both a bare httpx client and the
    DeliveryClient that wraps one. The escapes exist for the security tests: `body` sends bytes
    the payload could not express, `signature` sends a wrong one or none at all, and an empty
    `event` or `delivery` omits the header entirely.
    """
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if event:
        headers["X-GitHub-Event"] = event
    if delivery:
        headers["X-GitHub-Delivery"] = delivery

    header_signature = sign(raw, secret) if signature is ... else signature
    if header_signature is not None:
        headers["X-Hub-Signature-256"] = str(header_signature)

    return await client.post("/webhooks/github", content=raw, headers=headers)
