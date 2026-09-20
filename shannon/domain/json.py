"""What a decoded body is, before anything has been read out of it.

`json.loads` answers `Any`, and carrying that further hides a mistyped key name, or a field that
changed shape on GitHub's side, from the checker. The boundary is one step wide: a body arrives as
`object`, `is_json_object` narrows it to a mapping with the key type the format guarantees, and
everything after that is checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeGuard

# One level deep on purpose: nested objects are read back through this same guard, and a
# recursive alias would describe the same thing at the cost of one no checker enjoys expanding.
JsonObject = Mapping[str, object]


def is_json_object(value: object) -> TypeGuard[JsonObject]:
    """Whether a decoded value is an object, carrying the key type JSON guarantees.

    A `TypeGuard` rather than `isinstance` at each call site: the type parameters are erased at
    runtime, so `isinstance` narrows no further than `Mapping[Unknown, Unknown]`. That `str` is a
    fact about the format rather than an assumption about the payload.
    """
    return isinstance(value, Mapping)


def is_json_array(value: object) -> TypeGuard[Iterable[object]]:
    """Whether a decoded value is a list of things rather than one thing.

    Strings, bytes and mappings are iterable and none of them is an array.
    """
    return isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping)


def is_json_list(value: object) -> TypeGuard[list[object]]:
    """The same, for the places that also need to count what they were given.

    `Iterable` cannot be measured, and a decoded JSON array is always a `list`.
    """
    return isinstance(value, list)
