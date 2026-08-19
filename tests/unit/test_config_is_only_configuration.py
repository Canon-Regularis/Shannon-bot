"""`shannon/config.py` reads the environment and nothing else.

It used to import CommandRole to key a dict of role names, which made the settings module carry
a piece of the permission system. Nothing enforces that it stays clean except this.
"""

from __future__ import annotations

import ast
import pathlib


def test_config_imports_nothing_from_the_project() -> None:
    source = pathlib.Path("shannon/config.py").read_text(encoding="utf-8")

    inside = [
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("shannon")
    ]

    assert inside == [], (
        f"config.py imports {inside}. Settings reads the environment; anything that decides what "
        "a setting means belongs with the thing that means it."
    )
