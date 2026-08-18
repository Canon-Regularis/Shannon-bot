"""Every fake must offer everything the protocol it stands in for offers.

No type checker runs here and Protocols are structural, so a fake that drifts narrower than the
real thing fails nothing. It removes a path from what the suite can reach, and the suite keeps
reporting green over the hole.

Signatures are compared as well as names: a fake taking `thread` where the real one takes
`thread_id` will accept a call production would reject.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

import pytest

from shannon.api.dependencies import EventIntake
from shannon.api.routes.health import Liveness
from shannon.discord_bot.threads import (
    DiscordThreadGateway,
    LocksThread,
    OpensThreads,
    PostsToThread,
    ThreadGateway,
)
from shannon.github.client import GitHubClient, HttpGitHubClient
from shannon.github.webhooks.events import EventRouter
from shannon.runtime.liveness import ProcessLiveness
from shannon.services.delivery.queue import (
    DeliveryInbox,
    DeliveryQueue,
    WebhookDeliveryQueue,
)
from shannon.services.sync.items import (
    ItemSyncService,
    Notifier,
    OpensAndLocksThreads,
    SyncsItems,
    ThreadBinding,
)
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, SyncPolicy
from shannon.services.sync.threads import ItemThreads
from tests.fakes.github import FakeGitHubClient
from tests.fakes.queues import InMemoryDeliveryQueue
from tests.fakes.threads import FakeThreadGateway

# Both halves matter. The fakes are what the tests run against, and the real implementations are
# what production runs against, and neither is checked by anything else.
IMPLEMENTATIONS: list[tuple[type[Any], type[Any]]] = [
    (ThreadGateway, FakeThreadGateway),
    (ThreadGateway, DiscordThreadGateway),
    (OpensThreads, DiscordThreadGateway),
    (PostsToThread, DiscordThreadGateway),
    (LocksThread, DiscordThreadGateway),
    (OpensAndLocksThreads, DiscordThreadGateway),
    (GitHubClient, FakeGitHubClient),
    (GitHubClient, HttpGitHubClient),
    (DeliveryInbox, InMemoryDeliveryQueue),
    (DeliveryInbox, WebhookDeliveryQueue),
    (DeliveryQueue, WebhookDeliveryQueue),
    (SyncPolicy, PullRequestPolicy),
    (SyncPolicy, IssuePolicy),
    (Liveness, ProcessLiveness),
    (Notifier, ActorNotifier),
    (ThreadBinding, ItemThreads),
    (SyncsItems, ItemSyncService),
    (EventIntake, EventRouter),
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
