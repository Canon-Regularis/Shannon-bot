"""Reading a `check_suite` body, and every way of deciding there is nothing to do with one.

Issue #112. The refusal that matters most is the last one: a suite heading no pull request is
dropped rather than chased, because the endpoint that would chase it answers with the pull request
the commit was MERGED by, and acting on that would post CI results of `main` into the thread of
finished work and ring everybody who reviewed it.
"""

from __future__ import annotations

import pytest

from shannon.github.webhooks.checks import parse_check_suite_event
from shannon.github.webhooks.events import CHECK_SUITE_ACTIONS
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.unit


class TestWhatItReads:
    @pytest.mark.parametrize("action", sorted(CHECK_SUITE_ACTIONS))
    def test_every_supported_action_parses(self, action: str) -> None:
        assert parse_check_suite_event(action, payloads.check_suite_event(action)) is not None

    def test_the_commit_and_the_pull_requests(self) -> None:
        event = parse_check_suite_event("completed", payloads.check_suite_event())

        assert event is not None
        assert event.head_sha == payloads.CHECKED_SHA
        assert event.numbers == (7,)
        assert event.repository.full_name == f"{payloads.OWNER}/{payloads.REPO}"

    def test_a_branch_heading_two_pull_requests(self) -> None:
        """One into the default branch and one into a release branch. Both threads want it, and
        picking either would be arbitrary."""
        event = parse_check_suite_event("completed", payloads.check_suite_event(numbers=(7, 9)))

        assert event is not None
        assert event.numbers == (7, 9)

    def test_the_same_number_twice_is_read_once(self) -> None:
        event = parse_check_suite_event("completed", payloads.check_suite_event(numbers=(7, 7)))

        assert event is not None
        assert event.numbers == (7,)


class TestWhatItRefuses:
    @pytest.mark.parametrize("action", ["requested", "rerequested"])
    def test_a_suite_that_has_only_started(self, action: str) -> None:
        """CI starting is not something a thread has anything to say about, and acting on it would
        announce a result before there is one."""
        assert parse_check_suite_event(action, payloads.check_suite_event(action)) is None

    def test_no_repository(self) -> None:
        body = payloads.check_suite_event()
        del body["repository"]

        assert parse_check_suite_event("completed", body) is None

    def test_no_check_suite(self) -> None:
        body = payloads.check_suite_event()
        del body["check_suite"]

        assert parse_check_suite_event("completed", body) is None

    @pytest.mark.parametrize("head", [None, "", 12345])
    def test_no_usable_head_commit(self, head: object) -> None:
        body = payloads.check_suite_event()
        body["check_suite"]["head_sha"] = head

        assert parse_check_suite_event("completed", body) is None

    def test_a_suite_heading_no_pull_request(self) -> None:
        """A push to a branch nobody opened a pull request for, a push to the default branch, or a
        fork. See the module docstring for why there is no fallback."""
        assert parse_check_suite_event("completed", payloads.check_suite_event(numbers=())) is None

    @pytest.mark.parametrize("rows", [None, "seven", {"number": 7}])
    def test_pull_requests_that_is_not_a_list(self, rows: object) -> None:
        body = payloads.check_suite_event()
        body["check_suite"]["pull_requests"] = rows

        assert parse_check_suite_event("completed", body) is None

    def test_an_entry_with_no_number(self) -> None:
        body = payloads.check_suite_event()
        body["check_suite"]["pull_requests"] = [{"id": 1}, {"number": 7}]

        event = parse_check_suite_event("completed", body)

        assert event is not None
        assert event.numbers == (7,)

    def test_an_entry_that_is_not_an_object(self) -> None:
        body = payloads.check_suite_event()
        body["check_suite"]["pull_requests"] = ["seven", {"number": 7}]

        event = parse_check_suite_event("completed", body)

        assert event is not None
        assert event.numbers == (7,)


def test_it_never_raises_on_a_body_that_makes_no_sense() -> None:
    """A parser that raises takes the delivery down with it, and this one is handed whatever
    arrived over the network."""
    for body in ({}, {"check_suite": None}, {"check_suite": {"pull_requests": None}}):
        assert parse_check_suite_event("completed", body) is None
