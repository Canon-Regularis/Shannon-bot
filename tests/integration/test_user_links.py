from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.db.stores.user_links import LinkedAccount, UserLinkStore
from shannon.github.errors import GitHubUnavailableError
from shannon.services.linking import InvalidGitHubUsernameError, UserLinkingService
from tests.fakes.github import FakeGitHubClient

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker[AsyncSession]) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker, FakeGitHubClient())


async def count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(UserLink)) or 0


async def test_a_link_is_stored_lowercased(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    assert (
        await service.link(guild_id=1, github_username="OctoCat", discord_user_id=42) == "OctoCat"
    )

    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "octocat"
    assert row.discord_user_id == 42


async def test_an_at_prefix_is_stripped(service: UserLinkingService) -> None:
    assert (
        await service.link(guild_id=1, github_username="@octocat", discord_user_id=42) == "octocat"
    )


@pytest.mark.parametrize("username", ["not a name", "", "  ", "-leading", "a" * 40])
async def test_a_bad_username_is_refused(service: UserLinkingService, username: str) -> None:
    with pytest.raises(InvalidGitHubUsernameError):
        await service.link(guild_id=1, github_username=username, discord_user_id=42)


async def test_relinking_the_same_discord_account_replaces_the_github_name(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="monalisa", discord_user_id=42)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "monalisa"


async def test_moving_a_github_name_to_another_account_replaces_the_row(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="octocat", discord_user_id=99)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.discord_user_id == 99


async def test_links_are_scoped_to_a_guild(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=2, github_username="octocat", discord_user_id=77)

    assert await count(db_session) == 2


async def test_resolving_many_ignores_case_and_skips_unknowns(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)

    resolved = await UserLinkStore(db_session).resolve_many(
        guild_id=1, people={"OctoCat": None, "nobody": None}
    )

    assert resolved == {"octocat": 42}


async def test_resolving_nothing_asks_the_database_for_nothing(
    db_session: AsyncSession,
) -> None:
    assert await UserLinkStore(db_session).resolve_many(guild_id=1, people={}) == {}


class TestALoginNobodyHolds:
    """A link that can never match is the one outcome this command must not record.

    The fallback for somebody who has not linked is to name them in plain text, deliberately.
    So a login with a typo in it produces exactly what an unlinked person produces: plain text
    in the ping, plain text in the block, and nothing at all in the log. There is no way for the
    person, the admin or the owner to tell the two apart, and the person simply never hears from
    the bot again.

    Proven before it was fixed, driving the real command: `/link mona--lisa`, `/link monalisa-`
    and `/link definitely-not-a-real-account` were all answered "Linked GitHub user ... to
    <@555>.", and a review requested from the real `monalisa` went out as
    "Review requested from monalisa."
    """

    @pytest.fixture
    def github(self) -> FakeGitHubClient:
        return FakeGitHubClient(users={"monalisa": 900})

    async def test_a_login_github_has_never_heard_of_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], github: FakeGitHubClient
    ) -> None:
        service = UserLinkingService(db_sessionmaker, github)

        with pytest.raises(InvalidGitHubUsernameError, match="no user called"):
            await service.link(guild_id=1, github_username="monalisaa", discord_user_id=555)

    async def test_the_one_it_has_heard_of_is_linked(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        db_session,
    ) -> None:
        service = UserLinkingService(db_sessionmaker, github)

        assert await service.link(guild_id=1, github_username="MonaLisa", discord_user_id=555)
        assert github.user_calls == ["MonaLisa"]

    @pytest.mark.parametrize("typed", ["mona--lisa", "monalisa-", "-monalisa", "mona_lisa"])
    async def test_a_shape_github_cannot_issue_never_reaches_the_network(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        typed: str,
    ) -> None:
        """GitHub's rule is single hyphens, never leading or trailing. The pattern was looser,
        so these were stored; being narrower now also saves a call that could only say no."""
        service = UserLinkingService(db_sessionmaker, github)

        with pytest.raises(InvalidGitHubUsernameError, match="not a GitHub username"):
            await service.link(guild_id=1, github_username=typed, discord_user_id=555)

        assert github.user_calls == [], "it asked GitHub about a name it could rule out itself"

    async def test_github_being_unreachable_refuses_rather_than_guesses(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A link that cannot be checked is worth less than the person trying again in a minute,
        and the reply table already knows how to say GitHub could not be reached."""
        unreachable = FakeGitHubClient()
        unreachable.error = GitHubUnavailableError("Could not reach GitHub")
        service = UserLinkingService(db_sessionmaker, unreachable)

        with pytest.raises(GitHubUnavailableError):
            await service.link(guild_id=1, github_username="monalisa", discord_user_id=555)


class TestALoginThatChangedHands:
    """A login is not an identity, and this is the case that separates the two.

    GitHub frees an account name the moment it is renamed or deleted, redirects the old one so
    nothing appears to break, and lets anybody register it. A row matched on the name alone
    points at whoever holds it now rather than at the person somebody linked, so a stranger who
    took a freed name inherited the previous holder's Discord account everywhere a mention is
    built, including the ping, which is the one that actually notifies them.

    Alice linked as `alice`. She renames to `alicia` and nobody re-runs `/link`, because nothing
    anywhere says the link has gone stale. Months later a new contributor takes `alice`.
    """

    async def test_the_stranger_does_not_inherit_the_mention(
        self, db_session: AsyncSession
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="alice", github_user_id=111, discord_user_id=42
        )

        resolved = await UserLinkStore(db_session).resolve_many(guild_id=1, people={"alice": 900})

        assert resolved == {}, "somebody else's account was mentioned as Alice"

    async def test_the_person_who_linked_is_still_resolved(self, db_session: AsyncSession) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="alice", github_user_id=111, discord_user_id=42
        )

        resolved = await UserLinkStore(db_session).resolve_many(guild_id=1, people={"alice": 111})

        assert resolved == {"alice": 42}

    async def test_it_says_so_rather_than_going_quiet(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not mentioning them looks exactly like somebody who never linked, so the one line
        naming both accounts is all anybody has to tell the two apart."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="alice", github_user_id=111, discord_user_id=42
        )

        with caplog.at_level("WARNING"):
            await UserLinkStore(db_session).resolve_many(guild_id=1, people={"alice": 900})

        assert "somebody else now" in caplog.text
        assert "/link again" in caplog.text

    @pytest.mark.parametrize("asked", [111, 900, None])
    async def test_a_row_from_before_the_column_existed_still_works(
        self, db_session: AsyncSession, asked: int | None
    ) -> None:
        """Nothing can invent an id for a link made before this: GitHub can say what a login is
        called now, not what it was called when somebody bound it. A null is no evidence, and
        refusing on no evidence would take away mentions that work."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="alice", github_user_id=None, discord_user_id=42
        )

        resolved = await UserLinkStore(db_session).resolve_many(guild_id=1, people={"alice": asked})

        assert resolved == {"alice": 42}

    async def test_a_payload_that_carries_no_id_falls_back_to_the_name(
        self, db_session: AsyncSession
    ) -> None:
        """The other side of the same rule: a deleted account arrives with no id at all."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="alice", github_user_id=111, discord_user_id=42
        )

        resolved = await UserLinkStore(db_session).resolve_many(guild_id=1, people={"alice": None})

        assert resolved == {"alice": 42}


class TestWhatOneMemberClaimed:
    """Discord member to GitHub account, which is what a write to GitHub has to start from.

    Issue #106 for the direction, issue #133 for the second half of the answer. This used to hand
    back the login alone and say in its docstring that there was no id to hold it against. There
    was, one column over, and a collaborator whose login had moved was told he had no access to a
    repository he could write to.
    """

    async def test_a_member_who_has_linked(self, db_session: AsyncSession) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="OctoCat", github_user_id=111, discord_user_id=42
        )

        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=42)

        assert found == LinkedAccount(login="octocat", github_user_id=111), (
            "the store lowercases on the way in and must answer that way, id and all"
        )

    async def test_a_member_who_has_not(self, db_session: AsyncSession) -> None:
        assert await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=42) is None

    async def test_a_member_linked_in_another_server(self, db_session: AsyncSession) -> None:
        """Links are per guild, so one server's claim says nothing about another's."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )

        assert await UserLinkStore(db_session).account_for(guild_id=2, discord_user_id=42) is None

    async def test_it_follows_a_relink(self, db_session: AsyncSession) -> None:
        """`/link` deletes and reinserts rather than updating, so this must read the live row."""
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )
        await store.link(
            guild_id=1, github_username="monalisa", github_user_id=222, discord_user_id=42
        )

        found = await store.account_for(guild_id=1, discord_user_id=42)

        assert found == LinkedAccount(login="monalisa", github_user_id=222)

    async def test_two_members_do_not_collide(self, db_session: AsyncSession) -> None:
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )
        await store.link(
            guild_id=1, github_username="monalisa", github_user_id=222, discord_user_id=99
        )

        assert await store.account_for(guild_id=1, discord_user_id=42) == LinkedAccount(
            login="octocat", github_user_id=111
        )
        assert await store.account_for(guild_id=1, discord_user_id=99) == LinkedAccount(
            login="monalisa", github_user_id=222
        )

    async def test_a_row_from_before_the_column_answers_with_no_id(
        self, db_session: AsyncSession
    ) -> None:
        """Nothing can invent one, and a null has to reach the caller as a null: it means there
        is no way to tell whether the name has moved, which is not the same as knowing it has
        not."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="octocat", github_user_id=None, discord_user_id=42
        )

        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=42)

        assert found == LinkedAccount(login="octocat", github_user_id=None)


class TestFollowingARename:
    """Taking the login GitHub uses for an account now. Issue #133.

    The same move `RepositoryStore.follow_rename` makes for a repository, and for the same
    reason: the id is the identity and the name is a label GitHub hands back out.
    """

    async def test_the_row_takes_the_login_github_uses_now(self, db_session: AsyncSession) -> None:
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )

        await store.follow_rename(
            guild_id=1, discord_user_id=42, github_user_id=111, login="TheOctocat"
        )

        found = await store.account_for(guild_id=1, discord_user_id=42)
        assert found == LinkedAccount(login="theoctocat", github_user_id=111), (
            "lowercased on the way in, or GitHub's mixed case rewrites the row every command"
        )

    async def test_it_says_so_in_the_log(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A row rewritten by a command somebody ran about somebody else is worth one line."""
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )

        with caplog.at_level("INFO"):
            await store.follow_rename(
                guild_id=1, discord_user_id=42, github_user_id=111, login="wanderer"
            )

        assert "following the rename from octocat" in caplog.text

    async def test_a_member_who_relinked_in_the_meantime_is_left_alone(
        self, db_session: AsyncSession
    ) -> None:
        """A claim somebody stated deliberately beats a correction made in passing. The id no
        longer matches, which is how that is known without holding a lock across GitHub."""
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )
        await store.link(
            guild_id=1, github_username="monalisa", github_user_id=222, discord_user_id=42
        )

        await store.follow_rename(
            guild_id=1, discord_user_id=42, github_user_id=111, login="wanderer"
        )

        assert await store.account_for(guild_id=1, discord_user_id=42) == LinkedAccount(
            login="monalisa", github_user_id=222
        )

    async def test_a_login_another_member_already_holds_is_not_taken_from_them(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`link` would delete the row in the way. That is its prerogative, because it runs when
        somebody states a claim; this runs inside `/assign`, and a developer putting a colleague
        on an issue must not destroy a third person's link on the way past."""
        store = UserLinkStore(db_session)
        await store.link(
            guild_id=1, github_username="octocat", github_user_id=111, discord_user_id=42
        )
        await store.link(
            guild_id=1, github_username="monalisa", github_user_id=222, discord_user_id=99
        )

        with caplog.at_level("WARNING"):
            await store.follow_rename(
                guild_id=1, discord_user_id=42, github_user_id=111, login="monalisa"
            )

        assert await store.account_for(guild_id=1, discord_user_id=42) == LinkedAccount(
            login="octocat", github_user_id=111
        )
        assert await store.account_for(guild_id=1, discord_user_id=99) == LinkedAccount(
            login="monalisa", github_user_id=222
        )
        assert "wants relinking" in caplog.text


class TestSeveralAtOnce:
    """The batched half of the other direction, added for issue #121.

    A published transcript needs a login for its author and for everybody each message tagged, and
    asking one at a time turned a forty-line batch into several hundred queries.
    """

    async def test_it_answers_everybody_it_knows(self, db_session: AsyncSession) -> None:
        store = UserLinkStore(db_session)
        await store.link(guild_id=1, github_username="Alice", github_user_id=1, discord_user_id=11)
        await store.link(guild_id=1, github_username="Bob", github_user_id=2, discord_user_id=22)

        found = await store.logins_for(guild_id=1, discord_user_ids=[11, 22])

        assert found == {11: "alice", 22: "bob"}

    async def test_somebody_nobody_linked_is_simply_absent(self, db_session: AsyncSession) -> None:
        """The same answer `account_for` gives as None, in the shape a batch wants."""
        store = UserLinkStore(db_session)
        await store.link(guild_id=1, github_username="Alice", github_user_id=1, discord_user_id=11)

        found = await store.logins_for(guild_id=1, discord_user_ids=[11, 99])

        assert found == {11: "alice"}

    async def test_a_link_in_another_server_is_not_this_ones(
        self, db_session: AsyncSession
    ) -> None:
        store = UserLinkStore(db_session)
        await store.link(guild_id=2, github_username="Alice", github_user_id=1, discord_user_id=11)

        assert await store.logins_for(guild_id=1, discord_user_ids=[11]) == {}

    async def test_asking_about_nobody_answers_without_a_query(
        self, db_session: AsyncSession
    ) -> None:
        """Most messages tag nobody, so this is the common case rather than an edge one. Proved
        against a closed session, which cannot answer a query at all."""
        store = UserLinkStore(db_session)
        await db_session.close()

        assert await store.logins_for(guild_id=1, discord_user_ids=[]) == {}


class TestBindingAnAccountGitHubVouchedFor:
    """What `/verify` writes, as opposed to what `/link` records.

    `link` asks GitHub whether the login exists and takes somebody's word for whose it is. This
    takes both halves off `GET /user` answering about the account that had just authorised, so
    there is nothing left to check and nothing anybody could have got wrong.
    """

    async def test_it_records_both_halves(
        self, service: UserLinkingService, db_session: AsyncSession
    ) -> None:
        await service.bind(guild_id=1, discord_user_id=42, login="OctoCat", github_user_id=583231)

        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=42)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_it_asks_github_nothing(self, db_sessionmaker) -> None:
        """GitHub has already answered. Asking again whether that login exists would be asking it
        to confirm its own sentence, and it would spend a call on every /verify to do it."""
        github = FakeGitHubClient()
        service = UserLinkingService(db_sessionmaker, github)

        await service.bind(guild_id=1, discord_user_id=42, login="octocat", github_user_id=583231)

        assert github.user_calls == []

    async def test_the_login_comes_back_lowercased(self, service: UserLinkingService) -> None:
        """The store lowercases on the way in, and the reply names what was written down rather
        than what GitHub happened to capitalise."""
        said = await service.bind(
            guild_id=1, discord_user_id=42, login="OctoCat", github_user_id=583231
        )

        assert said == "octocat"

    async def test_a_proof_takes_a_login_somebody_else_had_claimed(
        self, service: UserLinkingService, db_session: AsyncSession
    ) -> None:
        """The case this command exists for. Somebody typed a login that was not theirs into
        `/link`, and the real owner has now signed in to GitHub and said so. A proof beats a
        claim, so the row that goes is the one nobody ever vouched for.
        """
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="octocat", github_user_id=583231, discord_user_id=99
        )
        await db_session.commit()

        await service.bind(guild_id=1, discord_user_id=42, login="octocat", github_user_id=583231)

        db_session.expunge_all()
        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=1, discord_user_id=42) == LinkedAccount(
            login="octocat", github_user_id=583231
        )
        assert await store.account_for(guild_id=1, discord_user_id=99) is None

    async def test_it_replaces_whatever_the_member_had_before(
        self, service: UserLinkingService, db_session: AsyncSession
    ) -> None:
        """The other way a wrong link gets fixed: the member was bound to somebody else's account
        and proving theirs moves them off it, without an admin typing anything."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="somebody-else", github_user_id=111, discord_user_id=42
        )
        await db_session.commit()

        await service.bind(guild_id=1, discord_user_id=42, login="octocat", github_user_id=583231)

        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=1, discord_user_id=42)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_proving_in_one_server_writes_nothing_in_another(
        self, service: UserLinkingService, db_session: AsyncSession
    ) -> None:
        """Links are per guild and so are proofs, which matches how the rest of the schema is
        keyed: somebody proving who they are in one server has said nothing to another."""
        await service.bind(guild_id=1, discord_user_id=42, login="octocat", github_user_id=583231)

        assert await UserLinkStore(db_session).account_for(guild_id=2, discord_user_id=42) is None
