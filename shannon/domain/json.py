"""What a decoded body is, before anything has been read out of it.

`json.loads` answers `Any`, and that one is honest: the bytes came off the network and nothing
about them is known yet. Carrying that `Any` any further is what is not. Every function below the
decode then reads fields off a value the checker cannot see into, so a mistyped key name is
invisible and a field that changed shape on GitHub's side is found by a user rather than by a test.

So the boundary is one step wide. A body arrives as `object`, `is_json_object` narrows it to a
mapping with the key type the format guarantees, and everything after that is checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeGuard

# One level deep on purpose. Nested objects are read back through this same guard, so a recursive
# alias would describe the same thing at the cost of an alias no checker enjoys expanding.
JsonObject = Mapping[str, object]


def is_json_object(value: object) -> TypeGuard[JsonObject]:
    """Whether a decoded value is an object, carrying the key type JSON guarantees.

    A `TypeGuard` rather than `isinstance` written out at each call site, because `isinstance`
    cannot narrow to anything better than `Mapping[Unknown, Unknown]`: the parameters are erased at
    runtime and there is nothing left there to test. That `str` is a fact about the format rather
    than an assumption about the payload, which is what makes stating it here rather than checking
    it the right trade.
    """
    return isinstance(value, Mapping)


def is_json_array(value: object) -> TypeGuard[Iterable[object]]:
    """Whether a decoded value is a list of things rather than one thing.

    Strings, bytes and mappings are iterable and none of them is an array, so all three are
    refused. That is the same test this replaced at each call site, kept exactly rather than
    tightened to `list`, because the point of the change is the type and not the behaviour.
    """
    return isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping)


def is_json_list(value: object) -> TypeGuard[list[object]]:
    """The same, for the places that also need to count what they were given.

    `Iterable` cannot be measured, and the two callers that ask how many commits or files there
    were have to be. Narrower than `is_json_array` on purpose: a decoded array is always a list.
    """
    return isinstance(value, list)
