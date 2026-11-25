"""`shannon/github/` is a client of GitHub and never of the database. #271.

What GitHub says is read here, and writing it down is a service's business. Two modules used to
hold both halves, so the package that talks to GitHub could not be used, or tested, without the
tables behind it. The full layer checker is #203; until it lands, this is what holds the rule.
"""

from __future__ import annotations

import ast
import pathlib

# Found from this file rather than the working directory, as `test_domain_imports_nothing_else`
# explains: a relative glob run from anywhere else matches nothing and passes.
GITHUB = pathlib.Path(__file__).resolve().parents[3] / "shannon" / "github"

# What `github` may reach inside this project. Everything else is a layer above it or the database.
_ALLOWED = ("shannon.config", "shannon.domain", "shannon.github")


def _refused(module: str) -> bool:
    """Whether a module is the database, or anything in this project that is not below `github`."""
    if module.partition(".")[0] == "sqlalchemy":
        return True
    if not (module == "shannon" or module.startswith(("shannon.", "."))):
        return False
    return not any(module == name or module.startswith(f"{name}.") for name in _ALLOWED)


def _imported(node: ast.AST) -> list[str]:
    """What one statement imports. A relative import is reported as written, and so refused:
    the package writes every import absolute, and a relative one could climb out of it."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return ["." * node.level + (node.module or "")]
    return []


def test_github_imports_no_database_and_nothing_above_it() -> None:
    paths = sorted(GITHUB.rglob("*.py"))
    assert paths, f"no github modules found under {GITHUB}"

    offenders = [
        (path.relative_to(GITHUB.parent).as_posix(), name)
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for name in _imported(node)
        if _refused(name)
    ]

    assert offenders == [], (
        f"github imports {offenders}. It reads what GitHub says; a service writes it down, so "
        "anything here that needs a table belongs in shannon/services."
    )
