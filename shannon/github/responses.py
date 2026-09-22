"""Reading a JSON body off an HTTP response.

Its own module because `domain.json` knows about decoded values and deliberately not httpx.
"""

from __future__ import annotations

from typing import Any

import httpx

from shannon.domain.json import JsonObject, is_json_object


def json_object(response: httpx.Response) -> JsonObject:
    """A JSON object, or an empty one. Used where a missing field is already handled below."""
    try:
        payload: Any = response.json()
    except ValueError:
        return {}
    return payload if is_json_object(payload) else {}
