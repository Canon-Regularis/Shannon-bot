"""Every annotation in the package names something that exists.

`from __future__ import annotations` is on everywhere, which makes annotations strings that are
never evaluated. That is what the project wants: forward references cost nothing and imports stay
acyclic. What comes with it is that a name which does not exist is not an error anywhere. Nothing
raises, ruff does not check types, and a hundred per cent branch coverage runs the line without
ever looking at what it says.

Two of those were found by hand in one afternoon, both years-old and both harmless only by luck:
a parameter annotated with a class that had been renamed, and an attribute read off a slotted
dataclass that never had it. The second is the one that bites, because a slotted class raises at
runtime, and the only reason it never did is that every route to the line is closed by an
invariant stated somewhere else entirely.

This resolves them all instead. It is the cheapest half of a type checker: it says nothing about
whether the types are right, only that they are real.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing

import shannon


def _ours(obj: object) -> bool:
    """Skip anything re-exported from a dependency.

    A store does `from sqlalchemy import select`, which puts `select` in that module's namespace,
    and SQLAlchemy's own annotations name private aliases that are not importable at runtime.
    Their annotations are their business.
    """
    module = getattr(obj, "__module__", "") or ""
    return module.startswith("shannon")


def _everything_annotated() -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    for module in pkgutil.walk_packages(shannon.__path__, "shannon."):
        loaded = importlib.import_module(module.name)
        for name, obj in vars(loaded).items():
            if not _ours(obj):
                continue
            if inspect.isfunction(obj):
                found.append((f"{module.name}.{name}", obj))
            elif inspect.isclass(obj):
                found.append((f"{module.name}.{name}", obj))
                found.extend(
                    (f"{module.name}.{name}.{attr}", value)
                    for attr, value in vars(obj).items()
                    if inspect.isfunction(value)
                )
    return found


def test_every_annotation_resolves() -> None:
    unresolvable: list[str] = []
    subjects = _everything_annotated()
    for where, obj in subjects:
        try:
            typing.get_type_hints(obj)
        except Exception as error:
            unresolvable.append(f"{where}: {error}")

    assert subjects, "nothing was checked, so this proves nothing"
    assert not unresolvable, "annotations naming something that does not exist:\n" + "\n".join(
        unresolvable
    )
