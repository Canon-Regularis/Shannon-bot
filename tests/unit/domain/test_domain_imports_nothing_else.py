"""`shannon/domain/` imports the standard library and itself, and nothing else. #270.

Every package depends on `domain`, so anything it imports every package then carries, and a
domain unit test needs no database or network only while it stays pure. The rule held by care
alone. The full layer checker is #203; until it lands, nothing enforces this except this test.
"""

from __future__ import annotations

import ast
import pathlib
import sys

# Found from this file rather than the working directory: run from anywhere else, a relative glob
# matches nothing and the test passes without having read a line.
DOMAIN = pathlib.Path(__file__).resolve().parents[3] / "shannon" / "domain"


def _allowed(module: str) -> bool:
    return (
        module.partition(".")[0] in sys.stdlib_module_names
        or module == "shannon.domain"
        or module.startswith("shannon.domain.")
    )


def _imported(node: ast.AST) -> list[str]:
    """What one statement imports, as it would be checked; a relative import is reported as is.

    `domain` writes every import absolute, and a relative one could climb out of the package:
    `from .. import config` reaches `shannon`. Reported rather than resolved, so it is rewritten
    absolute and checked like the rest.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return ["." * node.level + (node.module or "")]
        return [node.module or ""]
    return []


def test_domain_imports_only_the_standard_library_and_itself() -> None:
    paths = sorted(DOMAIN.rglob("*.py"))
    assert paths, f"no domain modules found under {DOMAIN}"

    offenders = [
        (path.relative_to(DOMAIN.parent).as_posix(), name)
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for name in _imported(node)
        if not _allowed(name)
    ]

    assert offenders == [], (
        f"domain imports {offenders}. It is pure so its unit tests need no database or network, "
        "and so every package can depend on it without carrying anything else along."
    )
