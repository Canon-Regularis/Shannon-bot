"""Which kinds of line a thread is told, and what it is told by default. Issue #163.

The values are pinned because they are stored: a renamed kind would quietly forget every choice
made under the old name. The defaults are pinned because they are the decision the issue asked
for, and they reach every thread that never chose.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shannon.domain.enums import ObjectType
from shannon.domain.tracking import DEFAULTS, HAPPENS_TO, TrackedEvent, Tracking


class TestTheKinds:
    def test_the_stored_names_are_these_and_no_others(self) -> None:
        assert [event.value for event in TrackedEvent] == [
            "comments",
            "labels",
            "commits",
            "checks",
            "approvals",
            "draft",
            "people",
            "state",
        ]

    def test_only_labels_and_commits_start_off(self) -> None:
        """They ring nobody and arrive by the dozen; every other kind rings somebody or says
        what became of the thread."""
        assert {event for event, on in DEFAULTS.items() if not on} == {
            TrackedEvent.LABELS,
            TrackedEvent.COMMITS,
        }
        assert set(DEFAULTS) == set(TrackedEvent)

    def test_every_kind_of_item_says_what_can_happen_to_it(self) -> None:
        assert set(HAPPENS_TO) == set(ObjectType)

    def test_a_pull_request_can_get_every_kind(self) -> None:
        assert HAPPENS_TO[ObjectType.PR] == tuple(TrackedEvent)

    def test_an_issue_has_no_commits_ci_approvals_or_draft(self) -> None:
        assert HAPPENS_TO[ObjectType.ISSUE] == (
            TrackedEvent.COMMENTS,
            TrackedEvent.LABELS,
            TrackedEvent.PEOPLE,
            TrackedEvent.STATE,
        )

    def test_a_board_card_has_fields_and_a_life_and_nothing_else(self) -> None:
        assert HAPPENS_TO[ObjectType.TICKET] == (TrackedEvent.LABELS, TrackedEvent.STATE)

    @pytest.mark.parametrize("kind", list(ObjectType))
    def test_each_listing_keeps_the_order_the_kinds_are_declared_in(self, kind: ObjectType) -> None:
        order = list(TrackedEvent)
        listed = HAPPENS_TO[kind]
        assert list(listed) == sorted(listed, key=order.index)


class TestWhatAThreadChose:
    @pytest.mark.parametrize("stored", [None, "labels", 3, ["labels"], {}])
    def test_nothing_usable_written_down_is_the_defaults(self, stored: object) -> None:
        tracking = Tracking.of(stored)

        assert tracking == Tracking()
        assert {event: tracking.wants(event) for event in TrackedEvent} == DEFAULTS

    def test_a_choice_overrides_the_default_either_way(self) -> None:
        tracking = Tracking.of({"labels": True, "comments": False})

        assert tracking.wants(TrackedEvent.LABELS) is True
        assert tracking.wants(TrackedEvent.COMMENTS) is False
        assert tracking.wants(TrackedEvent.COMMITS) is False, "untouched, so still the default"

    def test_a_choice_the_same_as_the_default_is_still_a_choice(self) -> None:
        """Kept, so a default changed in a later release does not move a thread that said."""
        assert Tracking.of({"checks": True}).on == frozenset({TrackedEvent.CHECKS})

    def test_a_key_this_release_does_not_know_is_ignored(self) -> None:
        """A kind added later survives a rollback by being skipped rather than refused."""
        assert Tracking.of({"reactions": True, "labels": True}) == Tracking(
            on=frozenset({TrackedEvent.LABELS})
        )

    @pytest.mark.parametrize("value", ["yes", 1, 0, None, "true"])
    def test_only_a_real_true_or_false_is_a_choice(self, value: object) -> None:
        """A hand-edited row cannot turn a line on by being merely truthy."""
        assert Tracking.of({"labels": value}) == Tracking()

    @given(st.dictionaries(st.sampled_from([event.value for event in TrackedEvent]), st.booleans()))
    def test_what_it_wants_is_what_was_chosen_or_else_the_default(
        self, stored: dict[str, bool]
    ) -> None:
        tracking = Tracking.of(stored)

        for event in TrackedEvent:
            assert tracking.wants(event) == stored.get(event.value, DEFAULTS[event])
