"""`/label` and `/unlabel`, against a stub.

Issue #104. Two things here are not like the other commands. The reply names the label as the
REPOSITORY spells it rather than as it was typed, which is the one visible sign that a typo was
caught. And these are the first commands with an autocomplete, which must answer fast and must
never raise, because Discord shows an empty box either way and nobody can tell those apart.
"""

from __future__ import annotations

import pytest

from shannon.commands.labels import (
    MOST_CHOICES,
    _suggesting,
    build_label_command,
    build_unlabel_command,
)
from shannon.commands.workflow import PRIORITY_COMMANDS, STATUS_COMMANDS
from shannon.domain.errors import RepositoryMismatchError
from shannon.services.workflow import _OWNED_BY, NotAnItemThreadError, WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

THREAD = 9001


def outcome(*, changed: bool = True, label: str = "bug") -> WorkflowOutcome:
    return WorkflowOutcome("acme/widget", 7, changed=changed, label=label)


class StubLabels:
    def __init__(
        self, *, result: WorkflowOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or outcome()
        self.error = error
        self.calls: list[tuple[int, str, bool]] = []

    async def set_label(self, *, thread_id: int, name: str, adding: bool) -> WorkflowOutcome:
        self.calls.append((thread_id, name, adding))
        if self.error is not None:
            raise self.error
        return self.result


class StubSuggestions:
    def __init__(self, names: tuple[str, ...] = (), error: Exception | None = None) -> None:
        self.names = names
        self.error = error

    async def labels_for_thread(self, thread_id: int) -> tuple[str, ...]:
        if self.error is not None:
            raise self.error
        return self.names


def run_it(
    *,
    service: StubLabels | None = None,
    who=None,
    channel_id: int | None = THREAD,
    removing: bool = False,
):
    service = service or StubLabels()
    build = build_unlabel_command if removing else build_label_command
    command = build(service, default_gate(), StubSuggestions())
    interaction = FakeInteraction(user=who or developer(), channel_id=channel_id)
    return command, interaction, service


class TestWhoMayRunIt:
    @pytest.mark.parametrize("who", [developer, project_manager, administrator])
    async def test_the_tiers_that_may(self, who) -> None:
        command, interaction, service = run_it(who=who())

        await command.callback(interaction, "bug")

        assert service.calls == [(THREAD, "bug", True)]

    async def test_anybody_else_is_refused_before_anything_is_written(self) -> None:
        from tests.unit.commands.conftest import member_with

        command, interaction, service = run_it(who=member_with("Reviewer"))

        await command.callback(interaction, "bug")

        assert "You need one of these roles" in interaction.reply
        assert service.calls == []


class TestWhereItHasToBeRun:
    async def test_outside_a_server(self) -> None:
        command, interaction, service = run_it()
        interaction.guild_id = None

        await command.callback(interaction, "bug")

        assert interaction.said == "Run this inside a server channel."
        assert service.calls == []

    async def test_with_no_channel_at_all(self) -> None:
        command, interaction, service = run_it(channel_id=None)

        await command.callback(interaction, "bug")

        assert interaction.said == "Run this inside the item's thread."
        assert service.calls == []

    async def test_unlabel_takes_the_other_direction(self) -> None:
        command, interaction, service = run_it(removing=True)

        await command.callback(interaction, "bug")

        assert service.calls == [(THREAD, "bug", False)]


class TestWhatItSays:
    async def test_a_label_put_on(self) -> None:
        command, interaction, _ = run_it()

        await command.callback(interaction, "bug")

        assert interaction.said == "Put `bug` on acme/widget#7."

    async def test_a_label_taken_off(self) -> None:
        command, interaction, _ = run_it(removing=True)

        await command.callback(interaction, "bug")

        assert interaction.said == "Took `bug` off acme/widget#7."

    async def test_it_names_the_spelling_the_repository_uses(self) -> None:
        """Typed `BUG`, wrote `bug`. Saying back what somebody typed would hide the one thing
        about that worth showing them."""
        command, interaction, _ = run_it(service=StubLabels(result=outcome(label="bug")))

        await command.callback(interaction, "BUG")

        assert "`bug`" in interaction.reply
        assert "BUG" not in interaction.reply

    async def test_one_the_item_already_has(self) -> None:
        command, interaction, _ = run_it(service=StubLabels(result=outcome(changed=False)))

        await command.callback(interaction, "bug")

        assert interaction.said == "acme/widget#7 already has the label `bug`, so nothing changed."

    async def test_one_the_item_does_not_have(self) -> None:
        command, interaction, _ = run_it(
            service=StubLabels(result=outcome(changed=False)), removing=True
        )

        await command.callback(interaction, "bug")

        assert (
            interaction.said == "acme/widget#7 does not have the label `bug`, so nothing changed."
        )


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (NotAnItemThreadError("Run this inside the thread of a tracked item."), "tracked item"),
            (RepositoryMismatchError("acme/widget is not that repository any more."), "any more"),
        ],
    )
    async def test_a_refusal_is_reported_in_its_own_words(
        self, error: Exception, expected: str
    ) -> None:
        command, interaction, _ = run_it(service=StubLabels(error=error))

        await command.callback(interaction, "bug")

        assert expected in interaction.reply

    async def test_anything_that_is_not_ours_is_left_to_the_handler(self) -> None:
        command, interaction, _ = run_it(service=StubLabels(error=RuntimeError("a bug")))

        with pytest.raises(RuntimeError, match="a bug"):
            await command.callback(interaction, "bug")


class TestThePicker:
    async def test_it_offers_what_the_repository_has(self) -> None:
        suggest = _suggesting(StubSuggestions(("bug", "documentation")))

        found = await suggest(FakeInteraction(channel_id=THREAD), "")

        assert [choice.value for choice in found] == ["bug", "documentation"]

    async def test_it_filters_on_what_has_been_typed(self) -> None:
        suggest = _suggesting(StubSuggestions(("bug", "documentation", "good first issue")))

        found = await suggest(FakeInteraction(channel_id=THREAD), "doc")

        assert [choice.value for choice in found] == ["documentation"]

    async def test_it_filters_without_regard_to_case(self) -> None:
        suggest = _suggesting(StubSuggestions(("Bug",)))

        found = await suggest(FakeInteraction(channel_id=THREAD), "bu")

        assert [choice.value for choice in found] == ["Bug"]

    async def test_it_stops_at_the_number_discord_will_take(self) -> None:
        """Discord answers an over-long list with an error rather than truncating it, so a
        repository with a real taxonomy would break the picker entirely."""
        suggest = _suggesting(StubSuggestions(tuple(f"label-{n}" for n in range(40))))

        found = await suggest(FakeInteraction(channel_id=THREAD), "")

        assert len(found) == MOST_CHOICES

    async def test_it_says_nothing_outside_a_thread(self) -> None:
        suggest = _suggesting(StubSuggestions(("bug",)))

        assert await suggest(FakeInteraction(channel_id=None), "") == []

    async def test_it_never_raises(self) -> None:
        """Discord shows an empty box whether the callback failed or the repository has no
        labels, and nobody can tell those apart. Raising would make a GitHub outage look like a
        repository with nothing to offer, and take the typed field down with it."""
        suggest = _suggesting(StubSuggestions(error=RuntimeError("GitHub is down")))

        assert await suggest(FakeInteraction(channel_id=THREAD), "bug") == []


def test_every_reserved_name_points_at_a_command_that_exists() -> None:
    """The refusal tells somebody which command owns the label they tried to set, and the mapping
    is written out rather than derived because MEDIUM's command is `set_med_priority` and not
    `set_medium_priority`. Derived, it would be wrong for exactly one of the eight.
    """
    owned = {command: value for value, command in _OWNED_BY.items()}

    assert owned == {**STATUS_COMMANDS, **PRIORITY_COMMANDS}
