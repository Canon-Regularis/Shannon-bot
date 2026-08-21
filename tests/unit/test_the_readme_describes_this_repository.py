"""The parts of the README that restate something the tree already decides.

Prose goes stale silently. Most of the README cannot be checked by a machine and should not be,
but three claims in it are restatements of facts that live elsewhere, and every one of them had
already drifted: the data model table was missing `team_links`, the revision range stopped at
`0007` while the tree carried eleven, and the settings table is the only place several settings
are written down at all.

Checked against the tree rather than against a copy, so the check cannot drift with the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from shannon.config import Settings
from shannon.db import models  # noqa: F401  (registers every table on the metadata below)
from shannon.db.base import Base

ROOT = Path(__file__).parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_table_is_in_the_data_model_table() -> None:
    """A table nobody documents is a table nobody knows to look in when something is wrong."""
    documented = set(re.findall(r"^\| `(\w+)` \|", README, re.M))
    missing = set(Base.metadata.tables) - documented

    assert not missing, f"undocumented tables: {sorted(missing)}"


def test_the_revision_range_reaches_the_last_migration() -> None:
    """The README names a range rather than a count, so a new migration silently falls outside."""
    revisions = sorted(
        path.name.split("_", 1)[0] for path in (ROOT / "migrations" / "versions").glob("[0-9]*.py")
    )
    stated = re.search(r"Alembic revisions `(\d+)` to `(\d+)`", README)

    assert stated is not None, "the README no longer states a revision range"
    assert stated.group(1) == revisions[0]
    assert stated.group(2) == revisions[-1], (
        f"the README stops at {stated.group(2)} and the tree reaches {revisions[-1]}"
    )


def test_every_setting_is_in_the_settings_table() -> None:
    """The README is where somebody deploying this finds out a setting exists at all."""
    documented = set(re.findall(r"`(SHANNON_[A-Z_]+)`", README))
    wanted = {f"SHANNON_{field.upper()}" for field in Settings.model_fields}

    assert not wanted - documented, f"undocumented settings: {sorted(wanted - documented)}"
