"""`board_owner`, the one place an absent board owner falls back to the repository's. #270.

Every board-owner lookup in `services/` depends on it, and only the integration tier reached it,
with None or a real owner. This states the Python contract only: the SQL fallback in the
repository store uses `coalesce`, which reads an empty owner differently, and is not claimed to
match.
"""

from __future__ import annotations

import pytest

from shannon.domain.board import board_owner


class TestWhoseBoardItIs:
    @pytest.mark.parametrize(
        ("project_owner", "repo_name", "expected"),
        [
            (None, "octo/repo", "octo"),
            ("acme", "octo/repo", "acme"),
        ],
        ids=["no owner recorded", "an owner recorded"],
    )
    def test_a_recorded_owner_wins_over_the_repositorys_own(
        self, project_owner: str | None, repo_name: str, expected: str
    ) -> None:
        assert board_owner(project_owner=project_owner, repo_name=repo_name) == expected

    def test_an_empty_owner_falls_back_like_no_owner(self) -> None:
        """An empty owner once sent a board write out with no credential. GitHub answered 401,
        and the poller read that as permanent and wrote the card off for good. Only the
        truthiness of the fallback holds that off: `is not None` would keep coverage at 100%
        and bring it back, since coverage does not count an `or` as a branch."""
        assert board_owner(project_owner="", repo_name="octo/repo") == "octo"

    def test_the_owner_is_cut_at_the_first_slash(self) -> None:
        assert board_owner(project_owner=None, repo_name="octo/re/po") == "octo"

    def test_a_repository_name_without_a_slash_is_its_own_owner(self) -> None:
        """Pinned as it behaves today: a name GitHub never gives is passed through whole."""
        assert board_owner(project_owner=None, repo_name="repo") == "repo"
