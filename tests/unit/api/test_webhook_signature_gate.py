from __future__ import annotations

from shannon.api.routes.webhooks import MAX_BODY_BYTES
from shannon.github.webhooks.signature import sign
from tests.support.webhooks import SECRET, RecordingHandler, build_client, post


async def test_missing_signature_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"}, signature=None)

    assert response.status_code == 401
    assert "missing" in response.json()["detail"]
    assert handler.calls == []


async def test_malformed_signature_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"}, signature="deadbeef")

    assert response.status_code == 401
    assert "malformed" in response.json()["detail"]
    assert handler.calls == []


async def test_signature_from_the_wrong_secret_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(
            client, "pull_request", {"action": "opened"}, secret="not-the-real-secret"
        )

    assert response.status_code == 401
    assert handler.calls == []


async def test_signature_over_a_different_body_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(
            client,
            "pull_request",
            body=b'{"action": "opened", "number": 7}',
            signature=sign(b'{"action": "opened"}', SECRET),
        )

    assert response.status_code == 401
    assert handler.calls == []


async def test_valid_signature_is_accepted(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"})

    assert response.status_code == 200
    assert handler.calls != []


async def test_unconfigured_secret_rejects_every_delivery(handler: RecordingHandler) -> None:
    async with build_client(handler, secret="") as client:
        response = await post(client, "pull_request", {"action": "opened"}, secret="")

    assert response.status_code == 500
    assert response.json()["detail"] == "Webhook secret is not configured"
    assert handler.calls == []


async def test_signature_is_checked_before_the_body_is_parsed(handler: RecordingHandler) -> None:
    """A forged request is turned away without its payload being trusted at all."""
    async with build_client(handler) as client:
        response = await post(client, "pull_request", body=b"{not json", signature=None)

    assert response.status_code == 401


async def test_a_body_larger_than_github_can_send_is_refused_on_its_declared_size() -> None:
    """The body is buffered whole before anything can be checked, so the cap comes first."""
    async with build_client(RecordingHandler()) as client:
        response = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_BODY_BYTES + 1),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "huge",
            },
        )

    assert response.status_code == 413


async def test_a_body_that_never_declares_its_size_is_still_refused() -> None:
    """Nothing obliges a client to send Content-Length, and this endpoint has to be reachable.

    Checking only the declared size means a chunked request without one is read to the end
    whatever its size, because the check passes on a header that is not there. No secret is
    needed to do it either: the signature covers the body, so it cannot be checked until the
    body is already in hand. Counting as it arrives is the only place the cap holds.
    """

    async def far_too_much():
        for _ in range((MAX_BODY_BYTES // (1024 * 1024)) + 4):
            yield b"a" * 1024 * 1024

    async with build_client(RecordingHandler()) as client:
        response = await client.post(
            "/webhooks/github",
            content=far_too_much(),
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "chunked",
            },
        )

    assert response.status_code == 413


async def test_an_ordinary_body_is_not_refused() -> None:
    async with build_client(RecordingHandler()) as client:
        response = await post(client, "pull_request", {"action": "opened"})

    assert response.status_code == 200
