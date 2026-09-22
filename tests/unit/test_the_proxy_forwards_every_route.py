"""Every route this app serves is one the proxy in front of it will forward.

Caddy is the only thing on that host facing the internet, and its config is an allowlist: two
`handle` blocks reverse-proxy to the app and everything else is answered with a bare 404. That is
the right shape — the docs endpoints are blocked deliberately and this asserts they stay blocked —
but it means a route can exist, be wired, be tested, and still not be reachable by anybody.

That is not hypothetical. `/oauth/github/callback` shipped, `/unregister` handed out links pointing
at it, and every one of them was answered by Caddy with an empty 404 because the allowlist had
never been widened. The bot never saw a single callback. Nothing failed anywhere a test could see:
the route answered fine in the suite, and in production the feature simply did not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shannon.api.app import create_app

pytestmark = pytest.mark.unit

# What FastAPI serves whether or not anybody asked for it, and what the Caddyfile blocks on
# purpose: the full API surface, its schemas and an interactive client against the one endpoint
# that does anything. Excluded here rather than forgotten, so this test says "these stay blocked"
# rather than going quiet about them.
SERVED_BUT_BLOCKED = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

# `handle <matcher> { ... reverse_proxy ... }`, which is the only shape in the file that sends a
# request onward. A `handle` answering with `respond 404` is matched and then dropped.
_FORWARDED = re.compile(r"handle\s+(\S+)\s*\{[^}]*reverse_proxy[^}]*\}", re.DOTALL)


def caddyfile() -> str:
    """Read by path, because it is deployed as a file rather than imported as anything.

    Held against the app rather than copied into this test: a copy is a copy that drifts, and what
    it has to get right is exactly the set this file has not been told about.
    """
    return (Path(__file__).parents[2] / "Caddyfile").read_text(encoding="utf-8")


def forwarded() -> list[str]:
    return _FORWARDED.findall(caddyfile())


def reaches_the_app(path: str) -> bool:
    """Whether Caddy would send this path onward, by its own matching rules.

    A matcher ending in `*` is a prefix and anything else is the whole path. That is the whole of
    what this file uses, and a matcher shape it does not cover should fail loudly here rather than
    be quietly read as a match.
    """
    for matcher in forwarded():
        assert matcher.startswith("/"), f"{matcher} is not a path matcher this test understands"
        if matcher.endswith("*"):
            if path.startswith(matcher[:-1]):
                return True
        elif path == matcher:
            return True
    return False


def routes() -> list[str]:
    """Every path this project declares, asked of FastAPI rather than worked out from the objects.

    `app.routes` is no use here: an included router arrives as one opaque entry that does not
    expose the routes inside it, so walking that list finds the four endpoints FastAPI mounts for
    itself and none of this project's. The schema is generated from the real routing table and
    names exactly what was declared.
    """
    return sorted(create_app().openapi()["paths"])


def test_the_app_still_serves_the_routes_this_is_about() -> None:
    """A guard on the guard, and it has already earned its place: the first way this file
    collected routes answered with an empty list, which made every assertion below pass."""
    live = routes()

    assert "/oauth/github/callback" in live
    assert "/webhooks/github" in live
    assert "/health" in live


def test_every_route_is_one_the_proxy_forwards() -> None:
    unreachable = [path for path in routes() if not reaches_the_app(path)]

    assert unreachable == [], (
        f"{unreachable} would be answered by Caddy rather than by this app, so they do not exist "
        "in production. Add a handle block to the Caddyfile."
    )


@pytest.mark.parametrize("path", sorted(SERVED_BUT_BLOCKED))
def test_the_endpoints_meant_to_be_blocked_still_are(path: str) -> None:
    """The other half. FastAPI mounts these unconditionally, nothing in this project needs them
    in production, and the endpoint they describe is where somebody would start."""
    assert not reaches_the_app(path), f"{path} is published to anyone who asks"


def test_nothing_is_forwarded_that_the_app_does_not_serve() -> None:
    """A matcher for a route that has gone is a hole nobody meant to leave open. Prefixes are
    checked by whether any live route sits under them, since that is what they are for."""
    live = routes()
    stray = [matcher for matcher in forwarded() if not any(_covers(matcher, path) for path in live)]

    assert stray == [], f"{stray} forwards to nothing this app serves"


def _covers(matcher: str, path: str) -> bool:
    return path.startswith(matcher[:-1]) if matcher.endswith("*") else path == matcher
