"""What Discord is told each command is called and asks for, written out in full. Issue #267.

`test_the_commands_discord_will_accept` holds every command to Discord's limits, and none of its
checks can see the values: a description reworded, an option renamed or made optional, or a
`guild_only` dropped all stay inside the limits. Those are what a member sees in the picker, and
what #267's refactor of the command factories could change by accident, so the whole surface is
pinned here as one table. A difference fails as a diff to read, and a deliberate change is made
by editing the table beside the code.

Choices are pinned where they are fixed at registration. An autocomplete offers its choices as
somebody types, so the table records that it suggests rather than what it would suggest.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest
from discord import app_commands
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from shannon.config import Settings
from shannon.container import build_container
from shannon.discord_bot.slash import Installable
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support.commands import runnable

pytestmark = pytest.mark.unit


class Option(NamedTuple):
    """One option as Discord registers it, in the order the command declares it."""

    name: str
    required: bool
    description: str
    choices: tuple[tuple[str, str], ...] = ()
    suggests: bool = False


class Registered(NamedTuple):
    """One runnable command as Discord registers it."""

    description: str
    guild_only: bool
    options: tuple[Option, ...] = ()


class Group(NamedTuple):
    """A group, which Discord registers with a description and no options of its own."""

    description: str
    guild_only: bool
    subcommands: tuple[str, ...]


ON_OFF = (("on", "on"), ("off", "off"))

GROUPS = {
    "board": Group(
        "The GitHub project board this server mirrors",
        True,
        ("link", "unlink", "authorise", "withdraw", "show"),
    ),
}

# Keyed by the name somebody types, so `/board link` is its own row. A subcommand reads as not
# guild-only because the flag is the group's, and Discord applies it to everything under it.
REGISTERED = {
    "register": Registered(
        "Bind a GitHub repository to this Discord server",
        True,
        (Option("github_repo_link", True, "Link to the GitHub repository"),),
    ),
    "unregister": Registered(
        "Unbind this server's GitHub repository",
        True,
        (
            Option(
                "repository",
                True,
                "The repository's full name, owner/name, to confirm you mean it",
            ),
        ),
    ),
    "set_channel": Registered(
        "Choose which channel a kind of GitHub item posts into",
        True,
        (
            Option(
                "object_type",
                True,
                "Which kind of GitHub item",
                (("pull requests", "PR"), ("issues", "ISSUE"), ("project tickets", "TICKET")),
            ),
            Option("channel", True, "Where its threads should appear"),
        ),
    ),
    "board link": Registered(
        "Mirror a GitHub project board in this server",
        False,
        (
            Option(
                "board",
                True,
                "The board to mirror: pick one, paste its URL, or type its number",
                suggests=True,
            ),
            Option("owner", False, "Only if the board is not owned by this repository's own owner"),
        ),
    ),
    "board unlink": Registered("Stop mirroring this server's board", False),
    "board authorise": Registered(
        "Let the cards you move from Discord move as you on GitHub", False
    ),
    "board withdraw": Registered("Take back the GitHub authorisation you gave this bot", False),
    "board show": Registered("Which board this server mirrors, and whose access reads it", False),
    "pr": Registered(
        "Sync a GitHub pull request into Discord",
        True,
        (Option("pr_link", True, "Link to the GitHub pull request"),),
    ),
    "issue": Registered(
        "Sync a GitHub issue into Discord",
        True,
        (Option("issue_link", True, "Link to the GitHub issue"),),
    ),
    "refresh": Registered(
        "Open threads for items that lack one, and redraw open pull requests and issues",
        True,
        (
            Option(
                "scope",
                False,
                "Which kinds of item to cover; leaving it out is the same as all",
                (
                    ("all", "everything"),
                    ("pull requests", "pull_requests"),
                    ("issues", "issues"),
                    ("tickets", "tickets"),
                ),
            ),
        ),
    ),
    "regenerate": Registered(
        "Redraw this item's details from GitHub, without pinging anybody", True
    ),
    "track": Registered(
        "Which kinds of line this thread gets, or turn one on or off",
        True,
        (
            Option(
                "event",
                False,
                "The kind of line; leave it out to see every kind and where it stands",
                (
                    ("comments", "comments"),
                    ("labels", "labels"),
                    ("commits", "commits"),
                    ("checks", "checks"),
                    ("approvals", "approvals"),
                    ("draft", "draft"),
                    ("people", "people"),
                    ("state", "state"),
                ),
            ),
            Option(
                "state",
                False,
                "on to post it here, off to stop; leave it out to see where it stands",
                ON_OFF,
            ),
        ),
    ),
    "link": Registered(
        "Connect your GitHub account to your Discord account",
        True,
        (Option("member", False, "Ask somebody else to connect theirs, in this channel"),),
    ),
    "link_team": Registered(
        "Connect a GitHub team to a Discord role",
        True,
        (
            Option("github_team", True, "The GitHub team, as it appears in the URL"),
            Option("role", True, "The Discord role to ping when that team is asked for a review"),
        ),
    ),
    "linked": Registered(
        "Whether you are linked to a GitHub account in this server, and which one", True
    ),
    "mentions": Registered(
        "Whether this bot's messages notify you in this server",
        True,
        (
            Option(
                "state", False, "on to be notified, off to be named without being notified", ON_OFF
            ),
        ),
    ),
    "remind": Registered(
        "Ping somebody here once, after the time you give",
        True,
        (
            Option("member", True, "Who to remind: you, or somebody else if your role allows it"),
            Option("days", False, "Days from now, up to 365"),
            Option("hours", False, "Hours on top of the days, up to 23"),
            Option("minutes", False, "Minutes on top of those, up to 59"),
            Option("message", False, "What to say with the ping, up to 500 characters"),
        ),
    ),
    "assign": Registered(
        "Put someone on this item as an assignee",
        True,
        (Option("member", True, "Who to put on this item"),),
    ),
    "unassign": Registered(
        "Take someone off this item's assignees",
        True,
        (Option("member", True, "Who to take off this item"),),
    ),
    "request_review": Registered(
        "Ask someone to review this pull request",
        True,
        (Option("member", True, "Who to ask for a review"),),
    ),
    "unrequest_review": Registered(
        "Withdraw a review request on this pull request",
        True,
        (Option("member", True, "Whose review request to withdraw"),),
    ),
    "label": Registered(
        "Put a label on this item",
        True,
        (Option("name", True, "Which label, from the ones this repository has", suggests=True),),
    ),
    "unlabel": Registered(
        "Take a label off this item",
        True,
        (Option("name", True, "Which label to remove", suggests=True),),
    ),
    "log_conversation": Registered("Publish this thread to the item's GitHub comments", True),
    "stop_conversation": Registered("Stop publishing this thread to GitHub", True),
    "status": Registered(
        "Move this item to a workflow status",
        True,
        (
            Option(
                "to",
                True,
                "A column on this server's board, or one of this bot's own names",
                suggests=True,
            ),
        ),
    ),
    "priority": Registered(
        "Set this item's priority",
        True,
        (
            Option(
                "to",
                True,
                "The priority to give it",
                (("High", "HIGH"), ("Medium", "MEDIUM"), ("Low", "LOW")),
            ),
        ),
    ),
}


def installed() -> list[Installable]:
    """The commands the container really installs, built as the Discord-limits test builds them.

    Against an engine that is never connected to: wiring is all this reads, so the table stays in
    the tier that runs without a database.
    """
    container = build_container(
        threads=FakeThreadGateway(),
        settings=Settings(github_webhook_secret=SecretStr("x")),
        engine=create_async_engine("postgresql+asyncpg://nobody@localhost/nothing"),
        github=FakeGitHubClient(),
    )
    return list(container.commands)


def as_registered(command: app_commands.Command[Any, ..., Any]) -> Registered:
    return Registered(
        command.description,
        command.guild_only,
        tuple(
            Option(
                parameter.name,
                parameter.required,
                parameter.description,
                tuple((choice.name, str(choice.value)) for choice in parameter.choices),
                parameter.autocomplete,
            )
            for parameter in command.parameters
        ),
    )


def test_every_command_is_registered_as_the_table_says() -> None:
    """Every command somebody can run, its words and its options, against the table above.

    One comparison rather than one test per command, so a refactor that touches several shows
    every difference at once instead of the first.
    """
    found = {command.qualified_name: as_registered(command) for command in runnable(installed())}

    assert found == REGISTERED


def test_every_group_is_registered_as_the_table_says() -> None:
    """`/board` itself: its own description, the guild-only flag its subcommands inherit, and
    which halves it holds, in the order Discord lists them."""
    found = {
        group.name: Group(
            group.description, group.guild_only, tuple(sub.name for sub in group.commands)
        )
        for group in installed()
        if isinstance(group, app_commands.Group)
    }

    assert found == GROUPS
