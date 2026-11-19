"""Keeping the OAuth `code` and `state` out of the access log (#266).

`oauth.py` promises that neither is ever logged, because its logs are the one place a credential
could come to rest. uvicorn broke that promise for it: its access logger writes every request's
path with the query string, at INFO, so each sign-in put a live `code` and a sealed `state` in the
production log. The `/oauth/` routes carry nothing in the query that is worth reading in a log, so
their access lines keep the path and lose the rest. Every other access line is left as it was.

This is the app's half. Caddy, in front of it on a server, keeps an access log of its own, and the
Caddyfile's log block drops the same two values, and the redirect's Location, from that one.
"""

from __future__ import annotations

import logging

ACCESS_LOGGER = "uvicorn.access"

# Where uvicorn's access line keeps the path: `'%s - "%s %s HTTP/%s" %d'` with client, method,
# path-with-query, HTTP version and status, in that order.
_PATH = 2
_ROUTES = "/oauth/"


class StripOAuthQuery(logging.Filter):
    """Drops the query string from an access line for an `/oauth/` route. Never drops a line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # Anything not shaped like uvicorn's access line is let through untouched: a filter that
        # guessed at a record it does not recognise could hide the line somebody needed.
        if isinstance(args, tuple) and len(args) > _PATH:
            path = args[_PATH]
            if isinstance(path, str) and path.startswith(_ROUTES):
                record.args = (*args[:_PATH], path.partition("?")[0], *args[_PATH + 1 :])
        return True


def keep_oauth_queries_out_of_the_access_log() -> None:
    """Install the filter on uvicorn's access logger, once however often this is called.

    On the logger rather than a handler, so it holds whichever handlers uvicorn's logging
    configuration attaches: that configuration replaces handlers and leaves filters alone.
    """
    access = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(installed, StripOAuthQuery) for installed in access.filters):
        access.addFilter(StripOAuthQuery())
