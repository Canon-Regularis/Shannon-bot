"""What uvicorn's access log may say about a sign-in (#266).

`oauth.py` promises that neither the `code` nor the `state` is ever logged. uvicorn's access line
writes the path with its query string at INFO, which put both in the production log on every
sign-in. These hold the filter that keeps them out, and that it touches nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from shannon.api.access_log import (
    ACCESS_LOGGER,
    StripOAuthQuery,
    keep_oauth_queries_out_of_the_access_log,
)
from tests.support.waiting import until

pytestmark = pytest.mark.unit

# uvicorn's own format for an access line.
FORMAT = '%s - "%s %s HTTP/%s" %d'


def access_line(path: str) -> logging.LogRecord:
    """A record shaped like the one uvicorn writes for one request."""
    return logging.LogRecord(
        ACCESS_LOGGER,
        logging.INFO,
        __file__,
        1,
        FORMAT,
        ("127.0.0.1:5000", "GET", path, "1.1", 200),
        None,
    )


@pytest.fixture
def access_logger() -> Iterator[logging.Logger]:
    """uvicorn's access logger, with whatever filters it had put back afterwards."""
    access = logging.getLogger(ACCESS_LOGGER)
    before = list(access.filters)
    yield access
    access.filters = before


class TestTheFilter:
    @pytest.mark.parametrize(
        "path",
        [
            "/oauth/start?state=SEALED",
            "/oauth/discord/callback?code=CODE&state=SEALED",
            "/oauth/github/callback?code=CODE&state=SEALED",
        ],
    )
    def test_a_sign_in_line_keeps_its_path_and_loses_its_query(self, path: str) -> None:
        record = access_line(path)

        assert StripOAuthQuery().filter(record) is True
        message = record.getMessage()
        assert path.partition("?")[0] in message
        assert "SEALED" not in message
        assert "CODE" not in message

    @pytest.mark.parametrize(
        "path", ["/webhooks/github", "/health", "/health?probe=1", "/oauthish?code=x"]
    )
    def test_every_other_line_is_left_as_it_was(self, path: str) -> None:
        record = access_line(path)

        assert StripOAuthQuery().filter(record) is True
        assert path in record.getMessage()

    @pytest.mark.parametrize(
        "args",
        [("only", "two"), ("127.0.0.1:5000", "GET", 404, "1.1", 200), {"path": "/oauth/x?code=y"}],
    )
    def test_a_line_it_does_not_recognise_is_let_through_untouched(self, args: object) -> None:
        """Guessing at a record of another shape could hide the line somebody needed."""
        record = logging.LogRecord(ACCESS_LOGGER, logging.INFO, __file__, 1, "%s", None, None)
        record.args = args  # type: ignore[assignment]

        assert StripOAuthQuery().filter(record) is True
        assert record.args == args


class TestInstallingIt:
    def test_it_is_installed_on_uvicorns_access_logger(self, access_logger: logging.Logger) -> None:
        access_logger.filters = []

        keep_oauth_queries_out_of_the_access_log()

        assert [type(installed) for installed in access_logger.filters] == [StripOAuthQuery]

    def test_installing_it_twice_installs_it_once(self, access_logger: logging.Logger) -> None:
        """`run` is called once in production, but more than once in a test session."""
        access_logger.filters = []

        keep_oauth_queries_out_of_the_access_log()
        keep_oauth_queries_out_of_the_access_log()

        assert len(access_logger.filters) == 1

    def test_a_line_through_the_logger_says_no_secret(
        self, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        access_logger.filters = []
        keep_oauth_queries_out_of_the_access_log()

        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            access_logger.info(
                FORMAT, "127.0.0.1:5000", "GET", "/oauth/github/callback?code=C&state=S", "1.1", 200
            )

        assert "/oauth/github/callback" in caplog.text
        assert "code=" not in caplog.text


class TestAgainstUvicornItself:
    """The tests above build uvicorn's record by hand, so they prove the filter strips a record of
    the shape it expects. This proves uvicorn still writes that shape, with both of its HTTP
    parsers: an upgrade that moved the path would otherwise let the query back into the log with
    every other test green."""

    @pytest.mark.parametrize("parser", ["h11", "httptools"])
    async def test_a_real_access_line_for_a_sign_in_keeps_no_secret(
        self, parser: str, access_logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def answer(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        access_logger.filters = []
        keep_oauth_queries_out_of_the_access_log()
        config = uvicorn.Config(
            answer, host="127.0.0.1", port=0, http=parser, log_config=None, lifespan="off"
        )
        server = uvicorn.Server(config)

        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            serving = asyncio.create_task(server.serve())
            await until(lambda: server.started)
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient() as client:
                await client.get(
                    f"http://127.0.0.1:{port}/oauth/github/callback?code=LIVE&state=SEALED"
                )
            server.should_exit = True
            await serving

        assert "/oauth/github/callback" in caplog.text
        assert "LIVE" not in caplog.text
        assert "SEALED" not in caplog.text
