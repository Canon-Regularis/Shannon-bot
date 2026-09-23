"""The rules every user-facing sentence in this project follows. Issue #147.

Twenty-five commands each worded their replies their own way, and nothing reconciled them: the
per-command tests pin what each one says, and none of them can see what the others say. So the
inconsistencies were not bugs anybody could have caught — they were invisible by construction.

What is held here is only what a machine can check, and the checks are read off the source with
`ast` rather than a regex. An f-string is a `JoinedStr` whose literal segments are in order, and
`!r` is a `FormattedValue` with `conversion == ord("r")`, so both are exact rather than guessed.

The first test is the one that pays for the file: the issue that asked for this standardisation
named `/set_medium_priority`, which has never existed. `services/workflow.py` already carries a
comment about that being the mistake this codebase makes, and now it cannot be made in a sentence.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

from shannon.config import Settings
from shannon.container import build_container
from shannon.discord_bot.responses import OWED, REFUSED, SUCCEEDED
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "shannon"

# Where a sentence somebody reads can be written. `github/` is left out of the command check
# alone, because its strings hold REST paths that look like commands and are not.
SPOKEN_TO = ("commands", "discord_bot", "services")

# The three ways a reply can end, and the calls that put a mark on one.
MARKS = (SUCCEEDED, OWED, REFUSED)
REPLY_CALLS = frozenset({"done", "owed", "refused"})

# A slash command, and not a path segment. What tells them apart is what follows: a command
# ends a word, and `/oauth/github/callback` and `/installations/new` carry on with another
# segment. The lookbehind keeps the middle of a URL out for the same reason.
_SLASH_COMMAND = re.compile(r"(?<![\w/])/([a-z][a-z0-9_]*)(?![\w/])")


def _modules(*packages: str) -> list[tuple[pathlib.Path, ast.Module]]:
    found: list[tuple[pathlib.Path, ast.Module]] = []
    for package in packages:
        for path in sorted((SOURCE / package).rglob("*.py")):
            found.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    return found


def _where(path: pathlib.Path, node: ast.AST) -> str:
    return f"{path.relative_to(SOURCE.parent)}:{getattr(node, 'lineno', '?')}"


def _literal_parts(node: ast.AST) -> list[str]:
    """Every literal run of an f-string or plain string, in reading order.

    Implicit concatenation arrives as one node already, so a sentence split across source lines
    is read whole rather than as fragments that each look unpunctuated.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def _docstrings(tree: ast.Module) -> set[int]:
    """Every docstring node in a module, by identity.

    Excluded from all of this. A docstring names commands freely and backticks them, and it is
    addressed to whoever is reading the code rather than to anybody in Discord.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _spoken(*packages: str) -> list[tuple[pathlib.Path, ast.AST, list[str]]]:
    """Every string a person could read, with its literal runs.

    Docstrings are out. Comments were never in, being absent from the tree. What is left is a
    close approximation rather than an exact set, and it errs towards including too much: the
    cost of that is a sentence in a log line held to the same standard as one in a reply, which
    is not a cost worth engineering around.
    """
    found: list[tuple[pathlib.Path, ast.AST, list[str]]] = []
    for path, tree in _modules(*packages):
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            parts = _literal_parts(node)
            if parts:
                found.append((path, node, parts))
    return found


def _installed_commands() -> set[str]:
    """Every name the container really registers, built against an engine nothing connects to."""
    container = build_container(
        threads=FakeThreadGateway(),
        settings=Settings(github_webhook_secret=SecretStr("x")),
        engine=create_async_engine("postgresql+asyncpg://nobody@localhost/nothing"),
        github=FakeGitHubClient(),
    )
    return {command.name for command in container.commands}


def test_every_command_a_sentence_names_is_one_that_exists() -> None:
    """The check this file is worth writing for.

    A reply that says "Run /set_channel first" is useless the moment the command is renamed, and
    nothing else would notice: the sentence still renders, the test still passes, and the person
    reading it goes looking for a command Discord has never heard of. The issue that asked for
    this standardisation made exactly that mistake in its own list.
    """
    installed = _installed_commands()
    named: dict[str, str] = {}
    for path, node, parts in _spoken(*SPOKEN_TO):
        for part in parts:
            for found in _SLASH_COMMAND.findall(part):
                named.setdefault(found, _where(path, node))

    unknown = {name: place for name, place in named.items() if name not in installed}
    assert not unknown, f"sentences name commands that do not exist: {unknown}"


def test_only_one_module_decides_what_a_mark_is() -> None:
    """The marks are put on in `responses.py` and nowhere else, so the three cannot drift.

    A command wording its own tick is how the project had half its refusals arriving uncoloured
    and unmarked before this: the tone lived at the call site, so there was no one place that
    could be made consistent.
    """
    loose: dict[str, str] = {}
    for path, node, parts in _spoken("commands", "services"):
        for part in parts:
            for mark in MARKS:
                if mark in part:
                    loose[mark] = _where(path, node)

    assert not loose, f"a mark is written outside discord_bot/responses.py: {loose}"


def test_nothing_a_person_reads_is_quoted_with_repr() -> None:
    """`!r` is Python's quoting, not a sentence's.

    It renders `'bug'` with straight quotes in the middle of English prose, and for a link or a
    label — which is every one of these — the project's own answer is a code span its own
    backticks cannot break out of.
    """
    reprs: dict[str, str] = {}
    for path, tree in _modules(*SPOKEN_TO, "github"):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = getattr(raised, "id", getattr(raised, "attr", ""))
            if not name.endswith("Error") or name in ("ValueError", "RuntimeError"):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.FormattedValue) and inner.conversion == ord("r"):
                    reprs[_where(path, inner)] = name

    assert not reprs, f"a refusal quotes with !r rather than a code span: {reprs}"


@pytest.mark.parametrize("state", ["status", "priority"])
def test_no_reply_spells_a_state_the_way_the_database_does(state: str) -> None:
    """`Status` and `Priority` are `StrEnum`, so `f"{status}"` leaks `READY_FOR_MERGE` just as
    surely as `{status.value}` does, and the second is the one a hand-written rule remembers.

    Both are caught here. `domain/`, `github/` and `db/` are not searched: the stored spelling is
    the truth there, and the log lines keep it on purpose so they stay greppable.
    """
    leaked: dict[str, str] = {}
    for path, tree in _modules("commands"):
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            inner = node.value
            if isinstance(inner, ast.Attribute) and inner.attr == "value":
                base = getattr(inner.value, "id", getattr(inner.value, "attr", ""))
                if base.lower().endswith(state):
                    leaked[_where(path, node)] = f"{base}.value"
            if isinstance(inner, ast.Name) and inner.id.lower().endswith(state):
                leaked[_where(path, node)] = inner.id

    assert not leaked, f"a reply spells a {state} the way the column does: {leaked}"


def test_a_reply_ends_in_a_full_stop_or_a_link() -> None:
    """Every sentence handed to `done`, `owed` or `refused` finishes.

    The carve-out is a reply that ends on what it is giving you — a thread mention, a URL — where
    a full stop lands against a link and reads as part of it. Those end on the value itself, and
    the literal before it ends in a colon or a newline, which is what marks them out.
    """
    unfinished: dict[str, str] = {}
    for path, tree in _modules("commands"):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", getattr(node.func, "attr", "")) not in REPLY_CALLS:
                continue
            for argument in node.args:
                parts = _literal_parts(argument)
                if not parts:
                    continue
                last = parts[-1].rstrip()
                # Ends on the value itself, so the literal before it says whether that was
                # meant: a colon or a line break introduces a link, anything else trails off.
                ends_on_a_value = isinstance(argument, ast.JoinedStr) and not isinstance(
                    argument.values[-1], ast.Constant
                )
                if ends_on_a_value and parts[-1].endswith((":", "\n", " ")):
                    continue
                if not last.endswith((".", "!", "?")):
                    unfinished[_where(path, node)] = parts[-1][-40:]

    assert not unfinished, f"a reply does not finish its sentence: {unfinished}"


def test_a_follow_up_command_is_named_one_way() -> None:
    """One verb and no backticks, so `Run /register first.` reads the same everywhere.

    `Use /x` and `` Run `/x` `` were both in use, in sentences sitting next to each other.
    """
    wrong: dict[str, str] = {}
    for path, node, parts in _spoken(*SPOKEN_TO):
        for part in parts:
            for found in re.finditer(r"(?:Use|use) /[a-z_]+|`/[a-z_]+`", part):
                wrong[_where(path, node)] = found.group(0)

    assert not wrong, f"a follow-up command is named another way: {wrong}"


def test_no_sentence_opens_with_a_mention() -> None:
    """`<#123>` and `<@456>` render as `#general` and `@alice`, so a sentence starting on one
    starts lowercase — and an id Discord cannot resolve renders as the raw angle brackets."""
    opening: dict[str, str] = {}
    for path, node, parts in _spoken(*SPOKEN_TO):
        if not isinstance(node, ast.JoinedStr) or not node.values:
            continue
        first = node.values[0]
        if not (isinstance(first, ast.Constant) and first.value in ("<#", "<@")):
            continue
        # A bare `f"<@{id}>"` is a mention being built, not a sentence beginning with one. What
        # tells them apart is prose after it, and prose has a space in it.
        rest = "".join(parts[1:]).lstrip(">").strip()
        # And a mention followed by a dash is a salutation, which is the one case where naming
        # somebody first is right: `/link @member` posts a note addressed to that person, and
        # burying the ping mid-sentence would hide it from the one reader it is for.
        if " " in rest and not rest.startswith(("—", "-", ":", ",")):
            opening[_where(path, node)] = f"{first.value}...{rest[:40]}"

    assert not opening, f"a sentence opens with a mention: {opening}"
