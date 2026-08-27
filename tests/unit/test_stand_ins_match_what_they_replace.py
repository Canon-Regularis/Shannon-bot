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
from shannon.commands.link import LinksAccounts
from shannon.commands.link_team import LinksTeams
from shannon.commands.register import RegistersRepositories
from shannon.commands.set_channel import MapsChannels
from shannon.commands.sync_link import SyncsByLink
from shannon.commands.workflow import MovesItems
from shannon.container import Container
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.permissions import RoleNames
from shannon.discord_bot.roles import ConfiguredRoles
from shannon.discord_bot.threads import (
    DiscordThreadGateway,
    LocksThread,
    OpensThreads,
    PostsToThread,
    ThreadGateway,
)
from shannon.github.client import GitHubClient, HttpGitHubClient, LooksUpUsers
from shannon.github.projects import HttpProjectBoards
from shannon.github.webhooks.events import EventHandler
from shannon.github.webhooks.router import EventRouter
from shannon.runtime.lifespan import Gateway, ProcessParts, RunsDeliveries
from shannon.runtime.liveness import ProcessLiveness
from shannon.services.channels import ChannelMappingService
from shannon.services.delivery.queue import (
    DeliveryInbox,
    DeliveryQueue,
    WebhookDeliveryQueue,
)
from shannon.services.delivery.worker import DeliveryWorker
from shannon.services.linking import TeamLinkingService, UserLinkingService
from shannon.services.notes import ItemNoteMirror, MirrorsNotes
from shannon.services.projects import ReadsBoards
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.sync.items import (
    ItemSyncService,
    Notifier,
    OpensAndLocksThreads,
    SyncsItems,
    ThreadBinding,
)
from shannon.services.sync.manual import ManualSync
from shannon.services.sync.notifications import ActorNotifier, ResolvesMentions
from shannon.services.sync.policies import (
    IssuePolicy,
    PullRequestPolicy,
    SyncPolicy,
    TicketPolicy,
)
from shannon.services.sync.threads import ItemThreads
from shannon.services.workflow import ItemWorkflow, LabelsItems
from tests.fakes.github import FakeGitHubClient
from tests.fakes.handlers import RecordingHandler
from tests.fakes.liveness import FakeLiveness
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
    (LooksUpUsers, FakeGitHubClient),
    (LooksUpUsers, HttpGitHubClient),
    (DeliveryInbox, InMemoryDeliveryQueue),
    (DeliveryInbox, WebhookDeliveryQueue),
    (DeliveryQueue, WebhookDeliveryQueue),
    (SyncPolicy, PullRequestPolicy),
    (SyncPolicy, IssuePolicy),
    (SyncPolicy, TicketPolicy),
    (Liveness, ProcessLiveness),
    (Notifier, ActorNotifier),
    (ThreadBinding, ItemThreads),
    (SyncsItems, ItemSyncService),
    (EventIntake, EventRouter),
    (Gateway, ShannonBot),
    (ProcessParts, Container),
    (RunsDeliveries, DeliveryWorker),
    (LinksAccounts, UserLinkingService),
    (LinksTeams, TeamLinkingService),
    (ResolvesMentions, UserLinkStore),
    (ResolvesMentions, TeamLinkStore),
    (RegistersRepositories, RepositoryRegistrationService),
    (MapsChannels, ChannelMappingService),
    (SyncsByLink, ManualSync),
    (MovesItems, ItemWorkflow),
    (LabelsItems, HttpGitHubClient),
    (LabelsItems, FakeGitHubClient),
    (ReadsBoards, HttpProjectBoards),
    (RoleNames, ConfiguredRoles),
    (MirrorsNotes, ItemNoteMirror),
    (Liveness, FakeLiveness),
    (EventHandler, RecordingHandler),
]


def parameters_of(member: Any) -> inspect.Signature | None:
    """The signature, or None for anything that is not a plain function.

    Properties and attributes have nothing to compare, and asking for a signature raises rather
    than returning something empty, so they are checked for presence only.
    """
    if not inspect.isfunction(member):
        return None
    return inspect.signature(member)


def accepts_everything_promised(promised: inspect.Signature, real: inspect.Signature) -> str | None:
    """Whether a call written against `promised` is one `real` will accept.

    Not equality. An implementation may take more than the protocol declares as long as the
    extra arguments have defaults, which is how `discord.Client.start(token, *, reconnect=True)`
    satisfies a protocol that only ever passes a token. What it may not do is rename a parameter
    the protocol names, or require one the protocol does not know to pass.
    """
    wanted = [n for n in promised.parameters if n != "self"]
    have = {n: p for n, p in real.parameters.items() if n != "self"}

    missing = [n for n in wanted if n not in have]
    if missing:
        return f"does not accept {missing}"

    required = [
        n
        for n, p in have.items()
        if n not in wanted
        and p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if required:
        return f"demands {required}, which no caller of the protocol knows to pass"
    return None


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
            promised = parameters_of(getattr(protocol, name, None))
            real = parameters_of(getattr(implementation, name, None))
            # None on either side means there is nothing to compare: a property, an attribute, or
            # a member the presence test above already reports on.
            if promised is None or real is None:
                continue
            complaint = accepts_everything_promised(promised, real)
            if complaint:
                differences.append(f"{name}: {complaint}")

        assert not differences, f"{describe(protocol, implementation)} differs: {differences}"
