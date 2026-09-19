"""The parts of the README that restate something the tree already decides.

Prose goes stale silently. Most of the README cannot be checked by a machine and should not be,
but four claims in it are restatements of facts that live elsewhere, and every one of them had
already drifted: the data model table was missing `team_links`, the revision range stopped at
`0007` while the tree carried eleven, and the settings table is the only place several settings
are written down at all.

The fourth is the webhook event list, added with issue #112. It is the one an operator ticks boxes
from, so a stale one is a feature that silently never fires, and it had been uncovered prose since
it was written.

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


# What the README's fenced list calls each event, against what `SUPPORTED_EVENTS` keys it as.
# Written out because the two are different vocabularies: GitHub's settings page offers "Pull
# requests" where the delivery header says `pull_request`, and an operator ticks the former.
WEBHOOK_NAMES = {
    "pull_request": "Pull requests",
    "issues": "Issues",
    "issue_comment": "Issue comments",
    "pull_request_review": "Pull request reviews",
    "pull_request_review_comment": "Pull request review comments",
    "check_suite": "Check suites",
}


def test_the_webhook_event_list_is_the_one_this_bot_acts_on() -> None:
    """The list somebody ticks boxes from.

    An event missing here is a feature that never fires and says nothing about why: the endpoint
    answers 200 `ignored`, writes no row, and the delivery is gone. Nothing else checks this.

    The two installation events are deliberately absent from the README's list and from this one.
    GitHub delivers them whether or not they are ticked, so telling anybody to tick them would be
    telling them to do something that changes nothing.
    """
    from shannon.github.webhooks.events import SUPPORTED_EVENTS

    listed = re.search(r"Choose individual events.*?```text\n(.*?)```", README, re.S)
    assert listed is not None, "the README no longer has a fenced webhook event list"
    ticked = set(listed.group(1).split("\n")) - {""}

    acted_on = {
        WEBHOOK_NAMES[event] for event in SUPPORTED_EVENTS if not event.startswith("installation")
    }
    assert ticked == acted_on, (
        "the README's webhook list and SUPPORTED_EVENTS disagree; "
        f"only in the README: {sorted(ticked - acted_on)}, "
        f"only in the code: {sorted(acted_on - ticked)}"
    )


def test_every_event_this_bot_acts_on_has_a_name_somebody_can_tick() -> None:
    """The table above is hand-written, so a new event with no entry would raise a KeyError inside
    the test above and report as an error rather than as the omission it is."""
    from shannon.github.webhooks.events import SUPPORTED_EVENTS

    unnamed = {
        event
        for event in SUPPORTED_EVENTS
        if not event.startswith("installation") and event not in WEBHOOK_NAMES
    }
    assert not unnamed, f"no README name for: {sorted(unnamed)}"
