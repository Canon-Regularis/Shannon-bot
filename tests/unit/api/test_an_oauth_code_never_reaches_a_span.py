"""FastAPI's own tracing stays off, so an OAuth callback's `code` never reaches a span. #253.

FastAPI 0.142 began tracing every request by itself the moment any OpenTelemetry provider is the
global one, and its server span carries the query string word for word: on
`/oauth/github/callback` that is the `code` and the `state` that `oauth.py` promises are never
logged. Nothing in this deployment sets a provider up, so nothing is recorded today. These hold
the promise for the day something does - an SDK, or `opentelemetry-instrument` in front of it.

The provider is put in by patching the API's global lookup rather than setting it, because the API
allows the global provider to be set once per process and a test must leave nothing behind.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.util import types
from pydantic import SecretStr

from shannon.api.app import create_app
from shannon.api.routes import oauth
from shannon.config import Settings

CALLBACK = "/oauth/github/callback?code=the-code&state=the-state"


class RecordingTracer(trace.NoOpTracer):
    """Starts the spans a no-op tracer would, keeping what each was asked to carry."""

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: types.Attributes = None,
        links: Sequence[trace.Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> trace.Span:
        self.started.append(dict(attributes or {}))
        return super().start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )


class OneTracerProvider(trace.TracerProvider):
    """A provider that is not one of the API's placeholders, so FastAPI takes it as configured."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: types.Attributes = None,
    ) -> trace.Tracer:
        return self._tracer


@pytest.fixture
def tracer(monkeypatch: pytest.MonkeyPatch) -> RecordingTracer:
    """A provider set up in the process, as an SDK would leave one."""
    recording = RecordingTracer()
    provider = OneTracerProvider(recording)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    return recording


async def call_back(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://shannon") as client:
        await client.get(CALLBACK)


async def test_a_provider_is_all_fastapi_needs_to_put_the_code_in_a_span(
    tracer: RecordingTracer,
) -> None:
    # The hazard on an app built without the switch, so the test below cannot pass by measuring
    # nothing at all.
    plain = FastAPI()
    plain.include_router(oauth.router)

    await call_back(plain)

    assert "code=the-code" in str([span.get("url.query") for span in tracer.started])


async def test_the_app_starts_no_span_whatever_provider_there_is(tracer: RecordingTracer) -> None:
    await call_back(create_app(settings=Settings(github_webhook_secret=SecretStr("x"))))

    assert tracer.started == []
