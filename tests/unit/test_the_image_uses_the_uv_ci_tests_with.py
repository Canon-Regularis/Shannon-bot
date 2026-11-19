"""The image is built by the uv CI tests with.

The Dockerfile copied uv 0.9.7 while every setup-uv step in CI and the Nix shell pinned 0.13.7, and
nothing said so. The image only uses uv to export the lock and install from it, but that is the
step that decides what ships, so "CI passed" was a claim about a different uv from the one that
built the image. flake.nix already holds the shell to CI's pins; this holds the image to them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def ci_pins() -> set[str]:
    """Every uv a setup-uv step in ci.yml is told to install.

    Read the way flake.nix reads it: only a line that is the bare `version:` key, so a
    `python-version:` or a commented-out pin is never mistaken for uv's.
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r'^ *version: "(\d+\.\d+\.\d+)"', ci, re.M))


def test_ci_pins_exactly_one_uv() -> None:
    """Two pins would leave the image nothing single to match, and none means the reading broke."""
    assert len(ci_pins()) == 1, f"ci.yml pins uv as {sorted(ci_pins())}"


def test_the_image_copies_the_uv_ci_pins() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = re.findall(r"ghcr\.io/astral-sh/uv:([0-9.]+)", dockerfile)

    assert copied, "the Dockerfile no longer copies uv from ghcr.io/astral-sh/uv"
    assert set(copied) == ci_pins(), (
        f"the Dockerfile copies uv {sorted(set(copied))} and ci.yml pins {sorted(ci_pins())}; "
        "move them together"
    )
