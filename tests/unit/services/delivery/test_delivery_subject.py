"""What a Delivery says about itself in a log line.

Here rather than in the integration tier: these build a dataclass and read a property, and a
unit test living in a file gated on PostgreSQL is a unit test nobody finds.
"""

from __future__ import annotations

from shannon.services.delivery.queue import Delivery


class TestWhatADeliveryIsAbout:
    """A delivery id names a row. An operator needs to know which repository and which item."""

    def test_it_names_the_event_the_repository_and_the_number(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc-123",
            event_type="pull_request",
            payload={
                "action": "opened",
                "repository": {"full_name": "acme/atlas"},
                "pull_request": {"number": 42},
            },
            attempts=0,
        )

        assert delivery.subject == "pull_request.opened acme/atlas#42"

    def test_a_comment_is_named_by_the_item_it_is_on(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issue_comment",
            payload={
                "action": "created",
                "repository": {"full_name": "acme/atlas"},
                "issue": {"number": 7},
            },
            attempts=0,
        )

        assert delivery.subject == "issue_comment.created acme/atlas#7"

    def test_a_payload_with_no_item_still_names_the_repository(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issues",
            payload={"action": "opened", "repository": {"full_name": "acme/atlas"}},
            attempts=0,
        )

        assert delivery.subject == "issues.opened acme/atlas"

    def test_a_payload_with_nothing_useful_says_what_it_can(self) -> None:
        delivery = Delivery(
            id=1, delivery_id="abc", event_type="ping", payload={"zen": "x"}, attempts=0
        )

        assert delivery.subject == "ping"

    def test_a_hostile_payload_does_not_break_it(self) -> None:
        """The payload is whatever arrived on the wire, so nothing here may assume a shape."""
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issues",
            payload={"repository": "not a dict", "issue": [1, 2, 3], "action": 7},
            attempts=0,
        )

        assert delivery.subject == "issues"
