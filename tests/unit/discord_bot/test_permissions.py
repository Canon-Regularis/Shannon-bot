from __future__ import annotations

import pytest

from shannon.commands._permissions import (
    BOARD_ROLES,
    REGISTER_ROLES,
    SYNC_ROLES,
    WORKFLOW_ROLES,
)
from shannon.config import Settings
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.permissions import MemberTiers, PermissionGate
from shannon.discord_bot.roles import CommandRole, ConfiguredRoles
from shannon.github import people
from shannon.github.errors import GitHubUnavailableError
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole
from tests.fakes.grants import FakeGrants


@pytest.fixture
def gate() -> PermissionGate:
    return PermissionGate(ConfiguredRoles.from_settings(Settings()))


def member(*role_names: str, administrator: bool = False) -> FakeMember:
    return FakeMember(
        roles=[FakeRole(name) for name in role_names],
        guild_permissions=FakeGuildPermissions(administrator=administrator),
    )


def test_guild_administrator_holds_admin(gate: PermissionGate) -> None:
    assert CommandRole.ADMIN in gate.roles_of(member(administrator=True))


def test_configured_role_names_map_to_tiers(gate: PermissionGate) -> None:
    held = gate.roles_of(member("Reviewer", "Developer"))

    assert held == {CommandRole.REVIEWER, CommandRole.DEVELOPER}


def test_role_matching_ignores_case_and_padding(gate: PermissionGate) -> None:
    assert gate.roles_of(member("  pROJECT manager  ")) == {CommandRole.PROJECT_MANAGER}


def test_a_configured_name_matches_however_either_side_is_cased_or_padded() -> None:
    gate = PermissionGate(ConfiguredRoles.from_settings(Settings(role_reviewer=" CODE owners ")))

    assert gate.roles_of(member("  code OWNERS")) == {CommandRole.REVIEWER}


def test_unrelated_roles_grant_nothing(gate: PermissionGate) -> None:
    assert gate.roles_of(member("Bots", "Gamers")) == frozenset()


def test_custom_role_names_from_configuration() -> None:
    gate = PermissionGate(
        ConfiguredRoles.from_settings(Settings(role_reviewer="Code Owners, Maintainers"))
    )

    assert gate.roles_of(member("Maintainers")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Code Owners")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Reviewer")) == frozenset()


def test_an_object_without_discord_attributes_is_rejected(gate: PermissionGate) -> None:
    assert gate.allows(object(), REGISTER_ROLES) is False


class Members:
    """Who is in the server by id, or a Discord that will not say, and who was asked about."""

    def __init__(self, *known: tuple[int, FakeMember], fails: Exception | None = None) -> None:
        self.known = dict(known)
        self.fails = fails
        self.asked: list[tuple[int, int]] = []

    async def member(self, *, guild_id: int, user_id: int) -> object | None:
        self.asked.append((guild_id, user_id))
        if self.fails is not None:
            raise self.fails
        return self.known.get(user_id)


class TestAskingDiscordWhetherAMemberStillHoldsATier:
    """`MemberTiers`, asked when a board link is followed. Found reviewing #201. It puts the member
    Discord has now through the same gate the command used, so the two answers compare."""

    async def test_a_member_still_holding_the_tier_holds_it(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Project Manager"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is True

    async def test_a_member_who_lost_it_does_not(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Developer"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False

    async def test_somebody_no_longer_in_the_server_holds_nothing(
        self, gate: PermissionGate
    ) -> None:
        tiers = MemberTiers(Members(), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False

    async def test_an_administrator_holds_every_tier_even_none(self, gate: PermissionGate) -> None:
        """Including the empty set, which is what a tier this code no longer knows narrows to."""
        tiers = MemberTiers(Members((555, member(administrator=True))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=frozenset()) is True

    async def test_the_empty_set_is_administrators_only(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Project Manager"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=frozenset()) is False

    async def test_discord_not_answering_is_not_an_answer(self, gate: PermissionGate) -> None:
        """Raised rather than read as no, so the caller can say which it was."""
        tiers = MemberTiers(Members(fails=DiscordGatewayError("Discord is down")), gate)

        with pytest.raises(DiscordGatewayError):
            await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES)

    async def test_it_asks_about_the_member_in_the_server_named(self, gate: PermissionGate) -> None:
        members = Members()

        await MemberTiers(members, gate).holds(
            guild_id=7, discord_user_id=555, tiers=REGISTER_ROLES
        )

        assert members.asked == [(7, 555)]


def gate_granting(
    grants: FakeGrants | None = None, *, settings: Settings | None = None
) -> PermissionGate:
    """The real gate on default role names, or on the ones given, with GitHub as a second route."""
    return PermissionGate(
        ConfiguredRoles.from_settings(settings or Settings()), grants or FakeGrants()
    )


class TestWhereGitHubCanStandIn:
    """Which permission GitHub would have to vouch for, gate by gate. Issue #94.

    Write stands in for Developer and admin for Project Manager as well. The Admin tier is never
    stood in for, since it passes every gate there is, and a tier a server emptied stays off.
    """

    @pytest.mark.parametrize(
        "allowed",
        [SYNC_ROLES, WORKFLOW_ROLES, REGISTER_ROLES, BOARD_ROLES],
        ids=["sync", "workflow", "register", "board"],
    )
    def test_a_gate_given_no_grants_has_no_route(
        self, gate: PermissionGate, allowed: frozenset[CommandRole]
    ) -> None:
        """What every server gets with the setting off: the gate it had before there was one."""
        assert gate.github_route(allowed) is None

    def test_a_developer_gate_takes_write(self) -> None:
        route = gate_granting().github_route(SYNC_ROLES)

        assert route is not None
        assert route.level == people.WRITE

    @pytest.mark.parametrize(
        "allowed",
        [WORKFLOW_ROLES, REGISTER_ROLES, BOARD_ROLES],
        ids=["workflow", "register", "board"],
    )
    def test_a_project_manager_gate_takes_admin(self, allowed: frozenset[CommandRole]) -> None:
        route = gate_granting().github_route(allowed)

        assert route is not None
        assert route.level == people.ADMIN

    @pytest.mark.parametrize(
        "allowed",
        [frozenset({CommandRole.ADMIN}), frozenset({CommandRole.REVIEWER}), frozenset()],
        ids=["admin", "reviewer", "administrators-only"],
    )
    def test_nothing_github_can_stand_in_for_has_no_route(
        self, allowed: frozenset[CommandRole]
    ) -> None:
        """The Admin tier passes every gate in `allows`, so standing in for it would let GitHub
        decide every command there is. Reviewer grants nothing, and the empty set is what an old
        board link with no stored tier narrows to, which stays administrators only."""
        assert gate_granting().github_route(allowed) is None

    def test_an_emptied_developer_setting_leaves_only_admin_for_a_developer_gate(self) -> None:
        """Emptying a role setting turns a tier off, and the route must not turn it back on."""
        route = gate_granting(settings=Settings(role_developer="")).github_route(SYNC_ROLES)

        assert route is not None
        assert route.level == people.ADMIN

    def test_every_tier_github_stands_in_for_emptied_leaves_no_route(self) -> None:
        gate = gate_granting(settings=Settings(role_developer=" , ", role_project_manager=""))

        assert gate.github_route(SYNC_ROLES) is None
        assert gate.github_route(REGISTER_ROLES) is None

    @pytest.mark.parametrize("developer", ["Developer", ""])
    @pytest.mark.parametrize("project_manager", ["Project Manager", ""])
    @pytest.mark.parametrize("admin", ["Admin", ""])
    @pytest.mark.parametrize(
        ("command", "allowed"),
        [
            ("pr", SYNC_ROLES),
            ("status", WORKFLOW_ROLES),
            ("unregister", REGISTER_ROLES),
            ("board show", BOARD_ROLES),
        ],
    )
    def test_a_route_always_has_a_role_to_follow(
        self,
        developer: str,
        project_manager: str,
        admin: str,
        command: str,
        allowed: frozenset[CommandRole],
    ) -> None:
        """What the route adds is "would let you in as well", which needs a role before it. A
        tier counts only while its setting names a role, so `denial` always has one to name."""
        gate = gate_granting(
            settings=Settings(
                role_developer=developer, role_project_manager=project_manager, role_admin=admin
            )
        )

        if gate.github_route(allowed) is not None:
            assert gate.denial(command, allowed).startswith("You need one of these roles")

    async def test_the_route_asks_at_its_level_and_passes_the_answer_back(self) -> None:
        grants = FakeGrants("GitHub says no.")
        route = gate_granting(grants).github_route(WORKFLOW_ROLES)
        assert route is not None

        said = await route.refusal_for(guild_id=7, discord_user_id=555)

        assert said == "GitHub says no."
        assert grants.asked == [(7, 555, people.ADMIN)]

    async def test_a_grant_comes_back_as_none(self) -> None:
        route = gate_granting(FakeGrants(None)).github_route(SYNC_ROLES)
        assert route is not None

        assert await route.refusal_for(guild_id=7, discord_user_id=555) is None

    async def test_github_not_answering_is_raised_rather_than_read_as_an_answer(self) -> None:
        """Every caller refuses on it. Folding it into a sentence here would let a caller that
        forgot to look read an outage as a reason, or worse, as nothing at all."""
        error = GitHubUnavailableError("GitHub is down")
        route = gate_granting(FakeGrants(error=error)).github_route(SYNC_ROLES)
        assert route is not None

        with pytest.raises(GitHubUnavailableError):
            await route.refusal_for(guild_id=7, discord_user_id=555)

    def test_the_roles_are_read_exactly_as_before_and_github_is_never_asked(self) -> None:
        """A role holder pays nothing for the route existing: `allows` and `denial` are still the
        synchronous checks they were, and asking which route there is asks nobody."""
        grants = FakeGrants(None)
        gate = gate_granting(grants)
        plain = PermissionGate(ConfiguredRoles.from_settings(Settings()))

        assert gate.allows(member("Developer"), SYNC_ROLES) is True
        assert gate.allows(member(), SYNC_ROLES) is False
        assert gate.denial("pr", SYNC_ROLES) == plain.denial("pr", SYNC_ROLES)
        gate.github_route(SYNC_ROLES)
        assert grants.asked == []


class TestABoardLinkFollowedLater:
    """`MemberTiers` on a gate with a GitHub route. Issue #94. A member GitHub let run `/board
    link` holds no role to be asked about when the link is followed, so GitHub is asked again."""

    async def test_a_role_holder_is_never_asked_about_on_github(self) -> None:
        grants = FakeGrants(None)
        tiers = MemberTiers(Members((555, member("Project Manager"))), gate_granting(grants))

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is True
        assert grants.asked == []

    async def test_a_member_github_vouches_for_holds_the_tier(self) -> None:
        grants = FakeGrants(None)
        tiers = MemberTiers(Members((555, member())), gate_granting(grants))

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is True
        assert grants.asked == [(1, 555, people.ADMIN)]

    async def test_a_member_github_turns_away_does_not(self) -> None:
        tiers = MemberTiers(Members((555, member())), gate_granting(FakeGrants("GitHub says no.")))

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False

    async def test_somebody_who_left_the_server_is_not_asked_about(self) -> None:
        grants = FakeGrants(None)
        tiers = MemberTiers(Members(), gate_granting(grants))

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False
        assert grants.asked == []

    async def test_a_link_from_before_the_tier_was_written_down_is_never_granted(self) -> None:
        """The empty set is administrators only, and GitHub stands in for no administrator."""
        grants = FakeGrants(None)
        tiers = MemberTiers(Members((555, member())), gate_granting(grants))

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=frozenset()) is False
        assert grants.asked == []

    async def test_github_not_answering_is_raised_rather_than_read_as_no(self) -> None:
        """So the page can say GitHub could not be asked, rather than that a role was lost."""
        error = GitHubUnavailableError("GitHub is down")
        tiers = MemberTiers(Members((555, member())), gate_granting(FakeGrants(error=error)))

        with pytest.raises(GitHubUnavailableError):
            await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES)


class TestWhetherGitHubCouldHaveLetThemIn:
    """`MemberTiers.github_could_stand_in`. Issue #94. It decides only which sentence a refused
    board link gets, so a deployment that never turned GitHub on keeps the sentence it had."""

    def test_a_gate_with_no_github_route_says_no(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members(), gate)

        assert tiers.github_could_stand_in(REGISTER_ROLES) is False

    def test_a_gate_with_a_route_for_those_tiers_says_yes(self) -> None:
        grants = FakeGrants(None)
        tiers = MemberTiers(Members(), gate_granting(grants))

        assert tiers.github_could_stand_in(REGISTER_ROLES) is True
        assert grants.asked == [], "saying whether GitHub could stand in asked it"

    def test_tiers_github_stands_in_for_none_of_say_no(self) -> None:
        """The empty set is administrators only."""
        tiers = MemberTiers(Members(), gate_granting(FakeGrants(None)))

        assert tiers.github_could_stand_in(frozenset()) is False
