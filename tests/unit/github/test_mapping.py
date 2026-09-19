"""Turning GitHub's JSON into this project's objects, when the JSON is not what it should be.

Every function here guards its fields, and until now nothing checked the guards. The parsers were
covered against payloads missing a key or shaped wrongly at the top, and a mutation campaign found
the gap that leaves: a field that is PRESENT and empty, or present and the wrong type, was let
through by six separate guards without a single test noticing. Turning each
`not isinstance(x, str) or not x` into an `and` left the whole suite green.

What that would cost is not an exception. It is an `Actor` whose login is the empty string
reaching the assignment store and the renderer, a repository with a blank name, and a label with
no text in a thread's tag line, none of which raises anywhere.

Whitespace is deliberately not in the lists below. These guards ask whether a value is a non-empty
string and nothing more, GitHub has no name made of spaces, and asserting a strip here would be
inventing a requirement rather than pinning one.
"""

from __future__ import annotations

import pytest

from shannon.domain.models import RepositorySnapshot
from shannon.github import mapping

REPO = RepositorySnapshot(
    github_repo_id=1, owner="acme", name="widget", html_url="https://github.com/acme/widget"
)

# Present, and no use. GitHub sends none of these today, which is exactly why a guard that stopped
# working would go unnoticed.
UNUSABLE_NAMES = ["", 0, 1, 12.5, None, [], {}, ["x"], {"login": "x"}]
UNUSABLE_URLS = ["", 7, None, [], {}]


class TestAnActor:
    @pytest.mark.parametrize("login", UNUSABLE_NAMES)
    def test_a_login_that_is_not_a_name_is_nobody(self, login: object) -> None:
        assert mapping.actor({"login": login, "id": 7}) is None

    def test_a_usable_login_still_maps(self) -> None:
        found = mapping.actor({"login": "octocat", "id": 7})

        assert found is not None
        assert (found.login, found.github_user_id) == ("octocat", 7)

    def test_an_id_that_is_not_a_number_is_dropped_without_losing_the_actor(self) -> None:
        """The login is what everything downstream matches on; the id is a convenience."""
        found = mapping.actor({"login": "octocat", "id": "seven"})

        assert found is not None
        assert found.github_user_id is None


class TestATeam:
    @pytest.mark.parametrize("slug", UNUSABLE_NAMES)
    def test_a_slug_that_is_not_a_name_is_no_team(self, slug: object) -> None:
        assert mapping.team({"slug": slug}) is None

    def test_the_name_is_read_only_when_there_is_no_slug(self) -> None:
        """A slug is stable; a name is a display string somebody can change."""
        assert mapping.team({"slug": "backend", "name": "Backend Team"}).login == "backend"
        assert mapping.team({"name": "Backend Team"}).login == "Backend Team"


class TestARepository:
    @pytest.mark.parametrize("name", UNUSABLE_NAMES)
    def test_a_name_that_is_not_a_name_is_no_repository(self, name: object) -> None:
        assert mapping.repository({"id": 5, "name": name, "owner": {"login": "acme"}}) is None

    @pytest.mark.parametrize("repo_id", ["5", "", None, 5.0, [], {}])
    def test_an_id_that_is_not_a_number_is_no_repository(self, repo_id: object) -> None:
        """The id is what an inbound webhook is resolved to a guild by, so a wrong one is worse
        than none: it would file the delivery under whichever repository happened to match."""
        payload = {"id": repo_id, "name": "widget", "owner": {"login": "acme"}}

        assert mapping.repository(payload) is None

    @pytest.mark.parametrize("url", UNUSABLE_URLS)
    def test_an_unusable_link_falls_back_to_one_that_works(self, url: object) -> None:
        found = mapping.repository(
            {"id": 5, "name": "widget", "owner": {"login": "acme"}, "html_url": url}
        )

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget"


class TestLabels:
    @pytest.mark.parametrize("name", UNUSABLE_NAMES)
    def test_a_label_with_no_usable_name_is_dropped_rather_than_rendered_blank(
        self, name: object
    ) -> None:
        assert mapping.labels([{"name": name}]) == ()

    def test_the_usable_ones_survive_beside_it(self) -> None:
        found = mapping.labels([{"name": "bug"}, {"name": ""}, {"name": "backend"}])

        assert [label.name for label in found] == ["bug", "backend"]


class TestAnItem:
    @pytest.mark.parametrize("url", UNUSABLE_URLS)
    def test_an_unusable_link_is_rebuilt_from_the_number(self, url: object) -> None:
        """The link is the one field of the metadata block a reader clicks, so a blank one is
        the difference between a thread that is useful and a thread that is not."""
        found = mapping.issue({"id": 5, "number": 7, "html_url": url}, REPO)

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget/issues/7"

    def test_a_pull_request_is_rebuilt_under_its_own_path(self) -> None:
        found = mapping.pull_request({"id": 5, "number": 7, "html_url": ""}, REPO)

        assert found is not None
        assert found.html_url == "https://github.com/acme/widget/pull/7"


class TestADescription:
    """The one field GitHub sends as a null rather than as an empty string.

    An item opened with the description box left alone arrives as `"body": null`, which is the
    ordinary case rather than a malformed payload, and a None reaching the renderer would be a
    `**Description:**` label over the word None.
    """

    @pytest.mark.parametrize("body", [None, 7, [], {}, True], ids=lambda v: type(v).__name__)
    def test_anything_that_is_not_text_reads_as_no_description(self, body: object) -> None:
        found = mapping.issue({"id": 5, "number": 7, "body": body}, REPO)

        assert found is not None
        assert found.body == ""

    def test_a_missing_key_reads_as_no_description(self) -> None:
        found = mapping.issue({"id": 5, "number": 7}, REPO)

        assert found is not None
        assert found.body == ""

    def test_text_is_carried_through_exactly_as_written(self) -> None:
        """Untouched here on purpose. What it looks like in Discord is decided where a reader is
        decided about, and cutting or tidying it here would leave nothing else able to."""
        written = "## Summary\n\n- one\n- two\n"

        found = mapping.issue({"id": 5, "number": 7, "body": written}, REPO)

        assert found is not None
        assert found.body == written

    def test_a_pull_request_carries_one_the_same_way(self) -> None:
        found = mapping.pull_request({"id": 5, "number": 7, "body": "why this exists"}, REPO)

        assert found is not None
        assert found.body == "why this exists"


# What GitHub actually sends for one row of a compare, trimmed to the fields anything reads. The
# two author blocks are both real and they disagree, which is the point: `commit.author.name` is
# whatever the pusher typed into their git config and nothing here may render it.
def commit_row(**overrides: object) -> dict[str, object]:
    row = {
        "sha": "a" * 40,
        "commit": {"message": "Add the endpoint\n\nAnswers the check.", "author": {"name": "Ada"}},
        "author": {"login": "octocat", "id": 583231},
        "parents": [{"sha": "b" * 40}],
    }
    row.update(overrides)
    return row


class TestOneCommitOffACompare:
    def test_the_account_is_read_and_the_git_name_is_not(self) -> None:
        """A security test wearing a parser's clothes. `commit.author.name` is free text from
        `git config user.name`, so anybody who can push could put a colleague's name against their
        own work. `author.login` is resolved by GitHub from the address and cannot be typed."""
        found = mapping.commit_ref(commit_row())

        assert found is not None
        assert found.author is not None
        assert found.author.login == "octocat"

    def test_a_commit_with_no_github_account_has_no_author(self) -> None:
        """GitHub sends null whenever the committing email is registered to nobody. It happens on
        ordinary work, so it has to read as an unknown person rather than as a broken row."""
        found = mapping.commit_ref(commit_row(author=None))

        assert found is not None
        assert found.author is None

    def test_a_commit_with_two_parents_is_a_merge(self) -> None:
        found = mapping.commit_ref(commit_row(parents=[{"sha": "b"}, {"sha": "c"}]))

        assert found is not None
        assert found.merge is True

    def test_a_commit_with_one_is_not(self) -> None:
        found = mapping.commit_ref(commit_row())

        assert found is not None
        assert found.merge is False

    def test_a_first_commit_with_no_parents_at_all_is_not_a_merge(self) -> None:
        found = mapping.commit_ref(commit_row(parents=[]))

        assert found is not None
        assert found.merge is False

    @pytest.mark.parametrize("sha", UNUSABLE_NAMES)
    def test_a_row_without_a_sha_is_no_commit(self, sha: object) -> None:
        """Nothing downstream could claim it: the note key is the SHA, so a row without one cannot
        be marked as said and would be posted again on every retry."""
        assert mapping.commit_ref(commit_row(sha=sha)) is None

    def test_a_row_with_no_message_still_reads(self) -> None:
        """An empty subject is a commit somebody can still be told about, and the SHA is the part
        that had to be there."""
        found = mapping.commit_ref(commit_row(commit={"message": None}))

        assert found is not None
        assert found.message == ""

    def test_a_body_that_is_not_an_object_reads_as_nothing(self) -> None:
        assert mapping.commit_ref("a" * 40) is None


class TestARangeOfCommits:
    def test_a_compare_body_reads_as_a_range(self) -> None:
        found = mapping.commit_range(
            {"status": "ahead", "total_commits": 2, "commits": [commit_row(), commit_row(sha="c")]}
        )

        assert found is not None
        assert found.status == "ahead"
        assert found.total == 2
        assert [commit.sha for commit in found.commits] == ["a" * 40, "c"]

    def test_a_commit_row_missing_a_sha_is_dropped_from_the_range(self) -> None:
        """One odd row does not lose the rest. The range is what the caller asked about, and
        refusing all of it because GitHub sent one entry nothing can use says less than it knows.
        """
        found = mapping.commit_range(
            {"status": "ahead", "total_commits": 2, "commits": [commit_row(sha=None), commit_row()]}
        )

        assert found is not None
        assert [commit.sha for commit in found.commits] == ["a" * 40]
        assert found.total == 2, "the count is what GitHub said, not a tally of what survived"

    def test_a_rollback_reads_as_behind_with_nothing_ahead(self) -> None:
        """The shape a `reset --hard && push --force` leaves, checked against the live API. It is
        the case that says nothing at all unless `behind` is treated as a rewrite."""
        found = mapping.commit_range({"status": "behind", "total_commits": 0, "commits": []})

        assert found is not None
        assert found.status == "behind"
        assert found.commits == ()

    @pytest.mark.parametrize("status", UNUSABLE_NAMES)
    def test_a_compare_with_no_status_is_no_range(self, status: object) -> None:
        """The status is the whole decision: announce, say it was force-pushed, or stay quiet.
        Guessing one would pick a branch of that on no evidence."""
        assert mapping.commit_range({"status": status, "commits": [], "total_commits": 0}) is None

    def test_a_missing_count_falls_back_to_what_was_listed(self) -> None:
        """Not to zero. The count is subtracted from to work out how many went unannounced, and a
        zero there reports every commit in the push as left out."""
        found = mapping.commit_range({"status": "ahead", "commits": [commit_row()]})

        assert found is not None
        assert found.total == 1

    @pytest.mark.parametrize("rows", [None, "commits", {}, 7])
    def test_a_commits_field_that_is_not_a_list_reads_as_no_commits(self, rows: object) -> None:
        found = mapping.commit_range({"status": "ahead", "total_commits": 4, "commits": rows})

        assert found is not None
        assert found.commits == ()

    def test_a_compare_body_that_is_not_an_object_reads_as_nothing(self) -> None:
        assert mapping.commit_range(["ahead"]) is None


class TestWhatOneCommitChanged:
    def test_the_numbers_are_read_off_the_stats_block(self) -> None:
        found = mapping.commit_stats({"stats": {"additions": 42, "deletions": 7}, "files": []})

        assert found is not None
        assert (found.additions, found.deletions) == (42, 7)

    def test_the_file_count_is_the_length_of_the_list_github_sent(self) -> None:
        """There is no `changed_files` on a commit, checked against the live API. The list stops
        at three hundred entries, so a very wide commit understates its files while its additions
        and deletions stay exact."""
        found = mapping.commit_stats(
            {"stats": {"additions": 1, "deletions": 0}, "files": [{}, {}, {}]}
        )

        assert found is not None
        assert found.changed_files == 3

    def test_no_files_block_counts_as_none_changed(self) -> None:
        found = mapping.commit_stats({"stats": {"additions": 1, "deletions": 0}})

        assert found is not None
        assert found.changed_files == 0

    def test_a_zero_is_a_number_and_not_a_missing_one(self) -> None:
        """A deletion-only commit has zero additions, which is the value every sloppy falsy check
        turns into nothing."""
        found = mapping.commit_stats({"stats": {"additions": 0, "deletions": 3}, "files": [{}]})

        assert found is not None
        assert found.additions == 0

    @pytest.mark.parametrize("value", ["", "4", None, [], {}, 4.5])
    def test_a_count_that_is_not_a_number_is_no_statistics(self, value: object) -> None:
        """Both ways round. Rendering `+None` or `+4.5` in a thread is worse than saying nothing,
        and the caller already has a path for a commit it cannot read."""
        assert mapping.commit_stats({"stats": {"additions": value, "deletions": 1}}) is None
        assert mapping.commit_stats({"stats": {"additions": 1, "deletions": value}}) is None

    def test_a_commit_with_no_stats_block_is_no_statistics(self) -> None:
        assert mapping.commit_stats({"files": [{}]}) is None

    @pytest.mark.parametrize("stats", [[], [1, 2], "42", 7])
    def test_a_stats_field_that_is_not_an_object_is_no_statistics(self, stats: object) -> None:
        """Separate from the missing case, because the two fail differently. A missing block falls
        through to the number check; a list walks straight into `.get` and raises."""
        assert mapping.commit_stats({"stats": stats}) is None

    def test_a_stats_body_that_is_not_an_object_reads_as_nothing(self) -> None:
        assert mapping.commit_stats(None) is None


class TestWhetherARepositoryIsPrivate:
    """Three states rather than two, and the third is the one that matters.

    GitHub sends the flag on every repository object it puts in a webhook payload, but the column
    behind this is nullable and reads a missing value as "nobody said" rather than as public.
    Collapsing the two would state something nobody checked, about the one question an operator
    asks of a deployment holding a backup.
    """

    def test_a_private_repository_reads_as_private(self) -> None:
        found = mapping.repository(
            {"id": 1, "name": "widget", "owner": {"login": "acme"}, "private": True}
        )

        assert found is not None
        assert found.private is True

    def test_a_public_one_reads_as_public(self) -> None:
        found = mapping.repository(
            {"id": 1, "name": "widget", "owner": {"login": "acme"}, "private": False}
        )

        assert found is not None
        assert found.private is False

    def test_a_body_that_does_not_say_reads_as_nobody_said(self) -> None:
        found = mapping.repository({"id": 1, "name": "widget", "owner": {"login": "acme"}})

        assert found is not None
        assert found.private is None

    @pytest.mark.parametrize("value", ["", "true", "false", 0, 1, None, [], {}])
    def test_anything_that_is_not_a_flag_reads_as_nobody_said(self, value: object) -> None:
        """Checked for the type rather than coerced. `bool(...)` would read `"false"` as private
        and a missing field as public, which are both worse than admitting to not knowing."""
        found = mapping.repository(
            {"id": 1, "name": "widget", "owner": {"login": "acme"}, "private": value}
        )

        assert found is not None
        assert found.private is None

    def test_visibility_does_not_decide_whether_the_repository_is_usable(self) -> None:
        """The flag is recorded, never a gate. A repository whose visibility cannot be read is
        still a repository, and refusing one here would be inventing a requirement."""
        assert (
            mapping.repository(
                {"id": 1, "name": "widget", "owner": {"login": "acme"}, "private": "nonsense"}
            )
            is not None
        )


class TestACheckRun:
    """Issue #112. A job's name is the whole of what a reader gets, so one without a usable name
    is dropped rather than rendered as an empty code span."""

    def test_a_usable_row(self) -> None:
        found = mapping.check_run(
            {
                "id": 7,
                "name": "Tests",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://x/job/7",
            }
        )

        assert found is not None
        assert (found.check_run_id, found.name, found.conclusion) == (7, "Tests", "failure")

    @pytest.mark.parametrize("row", [None, "Tests", 7, []])
    def test_a_row_that_is_not_an_object(self, row: object) -> None:
        assert mapping.check_run(row) is None

    @pytest.mark.parametrize("name", [None, "", 7])
    def test_a_row_with_no_usable_name(self, name: object) -> None:
        assert mapping.check_run({"id": 7, "name": name}) is None

    @pytest.mark.parametrize("number", [None, "7", 7.0])
    def test_a_row_with_no_usable_id(self, number: object) -> None:
        """The id is what the claim key is built from, and a run without one cannot be counted
        into it."""
        assert mapping.check_run({"id": number, "name": "Tests"}) is None

    def test_a_run_github_has_not_finished_describing(self) -> None:
        """An empty status reads as "not completed" and holds the whole announcement back, which
        is the safe direction: a result said late beats one said wrong."""
        found = mapping.check_run({"id": 7, "name": "Tests", "conclusion": None})

        assert found is not None
        assert (found.status, found.conclusion, found.html_url) == ("", "", "")


class TestAPageOfCheckRuns:
    def test_the_list_comes_out_of_the_wrapper(self) -> None:
        """This endpoint answers an object with the list under `check_runs`, unlike the labels
        list the client pages through beside it."""
        found = mapping.check_runs({"total_count": 1, "check_runs": [{"id": 1, "name": "CI"}]})

        assert [run.name for run in found] == ["CI"]

    @pytest.mark.parametrize("body", [None, [], "nothing", 7])
    def test_a_body_that_is_not_an_object(self, body: object) -> None:
        """An array is the shape a reader copied from the labels list would send, and it yields
        nothing rather than failing, which is why there is a test saying so."""
        assert mapping.check_runs(body) == []

    @pytest.mark.parametrize("rows", [None, {"id": 1}, "runs"])
    def test_a_body_whose_runs_are_not_a_list(self, rows: object) -> None:
        assert mapping.check_runs({"check_runs": rows}) == []

    def test_an_unusable_row_is_skipped_rather_than_failing_the_page(self) -> None:
        found = mapping.check_runs({"check_runs": [{"id": 1}, {"id": 2, "name": "CI"}, None]})

        assert [run.name for run in found] == ["CI"]


class TestAnAccountsPicture:
    """Issue #116. The guard is not about tidiness: Discord fetches a thumbnail's media itself and
    refuses the WHOLE message when it cannot, so a value that is merely odd rather than usable
    costs the item its block rather than costing a panel its picture."""

    def test_a_usable_avatar_is_carried(self) -> None:
        found = mapping.actor({"login": "octocat", "avatar_url": "https://example.invalid/u/1"})

        assert found is not None
        assert found.avatar_url == "https://example.invalid/u/1"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            12,
            ["https://example.invalid/u/1"],
            "http://example.invalid/u/1",
            "//example.invalid/u/1",
            "example.invalid/u/1",
            "javascript:alert(1)",
        ],
    )
    def test_anything_else_is_dropped(self, value: object) -> None:
        found = mapping.actor({"login": "octocat", "avatar_url": value})

        assert found is not None
        assert found.avatar_url is None

    def test_an_account_that_sent_none_is_still_an_account(self) -> None:
        """The picture is the only thing lost. A panel without one is a panel without a
        thumbnail, which is a different component tree and not a failure."""
        found = mapping.actor({"login": "octocat", "id": 7})

        assert found is not None
        assert (found.login, found.github_user_id, found.avatar_url) == ("octocat", 7, None)

    def test_a_team_never_has_one(self) -> None:
        """A team is carried as an Actor so one review request means one thing all the way
        through, but a review-request payload gives a team no picture."""
        found = mapping.team({"slug": "platform", "name": "Platform"})

        assert found is not None
        assert found.avatar_url is None
