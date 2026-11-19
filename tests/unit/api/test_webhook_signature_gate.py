from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from shannon.api.routes.webhooks import MAX_BODY_BYTES
from shannon.config import Settings
from shannon.github.webhooks.signature import sign
from tests.support.webhooks import SECRET, RecordingHandler, build_client, post

WEBHOOKS = "shannon.api.routes.webhooks"


@pytest.mark.parametrize(
    ("signature", "secret", "result", "detail"),
    [
        (None, SECRET, "MISSING", "X-Hub-Signature-256 header is missing"),
        ("deadbeef", SECRET, "MALFORMED", "X-Hub-Signature-256 header is malformed"),
        (..., "not-the-real-secret", "INVALID", "Signature does not match the request body"),
    ],
)
async def test_a_bad_signature_is_refused_with_its_reason(
    handler: RecordingHandler,
    caplog: pytest.LogCaptureFixture,
    signature: object,
    secret: str,
    result: str,
    detail: str,
) -> None:
    """Each refusal says which of the three it was, to GitHub's delivery log and to this one."""
    with caplog.at_level(logging.WARNING, logger=WEBHOOKS):
        async with build_client(handler) as client:
            response = await post(
                client, "pull_request", {"action": "opened"}, signature=signature, secret=secret
            )

    assert response.status_code == 401
    assert response.json()["detail"] == detail
    assert (WEBHOOKS, logging.WARNING, f"rejecting webhook delivery: {result}") in (
        caplog.record_tuples
    )
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


async def test_unconfigured_secret_rejects_every_delivery(
    handler: RecordingHandler, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=WEBHOOKS):
        async with build_client(handler, secret="") as client:
            response = await post(client, "pull_request", {"action": "opened"}, secret="")

    assert response.status_code == 500
    assert response.json()["detail"] == "Webhook secret is not configured"
    assert "neither SHANNON_GITHUB_APP_WEBHOOK_SECRET nor SHANNON_GITHUB_WEBHOOK_SECRET" in (
        caplog.text
    )
    assert handler.calls == []


class TestTheTwoSecrets:
    """The App's secret and the old per-repository one are both accepted while a deployment moves
    across. Every other test here sets only the old one, so these pin the App's (#266)."""

    async def test_the_apps_secret_alone_is_enough(self, handler: RecordingHandler) -> None:
        settings = Settings(github_app_webhook_secret=SecretStr("app-secret"))
        async with build_client(handler, settings=settings) as client:
            response = await post(client, "pull_request", {"action": "opened"}, secret="app-secret")

        assert response.status_code == 200
        assert handler.calls != []

    @pytest.mark.parametrize(("signed_with", "status"), [("app", 200), ("old", 200), ("x", 401)])
    async def test_with_both_set_either_one_signs(
        self, handler: RecordingHandler, signed_with: str, status: int
    ) -> None:
        settings = Settings(
            github_app_webhook_secret=SecretStr("app"), github_webhook_secret=SecretStr("old")
        )
        async with build_client(handler, settings=settings) as client:
            response = await post(client, "pull_request", {"action": "opened"}, secret=signed_with)

        assert response.status_code == status


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
    assert response.json()["detail"] == "Body is larger than GitHub will ever send"


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
    assert response.json()["detail"] == "Body is larger than GitHub will ever send"


async def test_an_ordinary_body_is_not_refused() -> None:
    async with build_client(RecordingHandler()) as client:
        response = await post(client, "pull_request", {"action": "opened"})

    assert response.status_code == 200
