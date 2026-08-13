"""Every fake must offer everything the protocol it stands in for offers.

There is no type checker in this project, and Protocols are structural, so nothing otherwise
notices when a fake drifts narrower than the real thing. That is not a hypothetical: it has
happened three times here. A thread gateway whose docstring described behaviour it did not
have. A GitHub client with no `get_issue`, which meant nothing could drive `/issue` at all and
the gap showed up as good coverage. A lifespan container with three of seventeen attributes.

None of those failed a test. They quietly removed a path from what the suite could reach, which
is worse than failing, because the suite kept reporting green over the hole.

Signatures are compared as well as names. A fake that takes `thread` where the real one takes
`thread_id` is a fake that will accept a call production would reject.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import pytest

from shannon.api.routes.health import Liveness
from shannon.discord_bot.threads import DiscordThreadGateway, ThreadGateway
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.main import ProcessLiveness
from shannon.services.delivery_queue import DeliveryQueue, WebhookDeliveryQueue
from shannon.services.policies import IssuePolicy, PullRequestPolicy, SyncPolicy
from tests.fakes.github import FakeGitHubClient
from tests.fakes.queues import InMemoryDeliveryQueue
from tests.fakes.threads import FakeThreadGateway

# Both halves matter. The fakes are what the tests run against, and the real implementations are
# what production runs against, and neither is checked by anything else.
IMPLEMENTATIONS: list[tuple[type[Any], type[Any]]] = [
    (ThreadGateway, FakeThreadGateway),
    (ThreadGateway, DiscordThreadGateway),
    (GitHubClient, FakeGitHubClient),
    (GitHubClient, HttpGitHubClient),
    (DeliveryQueue, InMemoryDeliveryQueue),
    (DeliveryQueue, WebhookDeliveryQueue),
    (SyncPolicy, PullRequestPolicy),
    (SyncPolicy, IssuePolicy),
    (Liveness, ProcessLiveness),
]


def parameters_of(member: Any) -> list[str] | None:
    """Parameter names, or None for anything that is not a plain function.

    Properties and attributes have no signature to compare, and asking for one raises rather
    than returning something empty, so they are checked for presence only.
    """
    if not inspect.isfunction(member):
        return None
    return [name for name in inspect.signature(member).parameters if name != "self"]


def describe(protocol: type[Any], implementation: type[Any]) -> str:
    return f"{implementation.__name__} against {protocol.__name__}"


@pytest.mark.parametrize(
    ("protocol", "implementation"), IMPLEMENTATIONS, ids=lambda value: value.__name__
)
class TestNothingIsNarrowerThanWhatItReplaces:
    def test_every_member_exists(self, protocol: type[Protocol], implementation: type[Any]) -> None:
        missing = sorted(
            name for name in protocol.__protocol_attrs__ if not hasattr(implementation, name)
        )

        assert not missing, (
            f"{describe(protocol, implementation)} is missing {missing}. Anything reaching for "
            f"those goes untested rather than failing."
        )

    def test_every_member_takes_the_same_arguments(
        self, protocol: type[Protocol], implementation: type[Any]
    ) -> None:
        differences = []
        for name in sorted(protocol.__protocol_attrs__):
            expected = parameters_of(getattr(protocol, name, None))
            actual = parameters_of(getattr(implementation, name, None))
            # None on either side means there is nothing to compare: a property, an attribute, or
            # a member the presence test above already reports on.
            if expected is None or actual is None or expected == actual:
                continue
            differences.append(f"{name}: expected {expected}, got {actual}")

        assert not differences, f"{describe(protocol, implementation)} differs: {differences}"
