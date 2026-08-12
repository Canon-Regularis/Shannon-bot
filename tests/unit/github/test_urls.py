from __future__ import annotations

import pytest

from shannon.domain.errors import UnparseableLinkError
from shannon.github.urls import parse_issue_url, parse_pull_request_url


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        "http://github.com/Canon-Regularis/Shannon-bot/pull/7",
        "https://www.github.com/Canon-Regularis/Shannon-bot/pull/7",
        "github.com/Canon-Regularis/Shannon-bot/pull/7",
        "https://github.com/Canon-Regularis/Shannon-bot/pull/7/",
        "https://github.com/Canon-Regularis/Shannon-bot/pull/7/files",
        "https://github.com/Canon-Regularis/Shannon-bot/pull/7#discussion_r1",
        "https://github.com/Canon-Regularis/Shannon-bot/pull/7?w=1",
        "  https://github.com/Canon-Regularis/Shannon-bot/pull/7  ",
        "<https://github.com/Canon-Regularis/Shannon-bot/pull/7>",
    ],
)
def test_valid_links_parse(link: str) -> None:
    ref = parse_pull_request_url(link)

    assert ref.owner == "Canon-Regularis"
    assert ref.name == "Shannon-bot"
    assert ref.number == 7
    assert ref.full_name == "Canon-Regularis/Shannon-bot"


def test_repository_names_with_dots_and_underscores_parse() -> None:
    ref = parse_pull_request_url("https://github.com/some-owner/my_repo.v2/pull/42")

    assert ref.name == "my_repo.v2"
    assert ref.number == 42


def test_large_pull_request_numbers_parse() -> None:
    assert parse_pull_request_url("https://github.com/a/b/pull/999999").number == 999999


def test_issue_link_is_rejected_by_name() -> None:
    with pytest.raises(UnparseableLinkError, match="is an issue link"):
        parse_pull_request_url("https://github.com/Canon-Regularis/Shannon-bot/issues/7")


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("", "No link was given"),
        ("   ", "No link was given"),
        ("https://gitlab.com/owner/repo/pull/7", "is not a github.com link"),
        ("https://github.example.com/owner/repo/pull/7", "is not a github.com link"),
        ("ftp://github.com/owner/repo/pull/7", "is not an http or https link"),
        ("https://github.com/owner", "does not contain an owner and repository"),
        ("https://github.com/owner/repo", "does not point at a pull request"),
        ("https://github.com/owner/repo/pull", "does not point at a pull request"),
        ("https://github.com/owner/repo/pull/", "does not point at a pull request"),
        ("https://github.com/owner/repo/commit/abc123", "does not point at a pull request"),
        ("https://github.com/owner/repo/pull/abc", "has no valid number"),
        ("https://github.com/owner/repo/pull/0", "has no valid number"),
        ("https://github.com/owner/repo/pull/-1", "has no valid number"),
        ("https://github.com/-bad/repo/pull/1", "is not a valid GitHub owner"),
        ("not a link at all", "is not a github.com link"),
    ],
)
def test_invalid_links_are_rejected(link: str, message: str) -> None:
    with pytest.raises(UnparseableLinkError, match=message):
        parse_pull_request_url(link)


def test_git_suffix_is_stripped() -> None:
    ref = parse_pull_request_url("https://github.com/owner/repo.git/pull/7")

    assert ref.name == "repo"


def test_owner_longer_than_github_allows_is_rejected() -> None:
    with pytest.raises(UnparseableLinkError, match="is not a valid GitHub owner"):
        parse_pull_request_url(f"https://github.com/{'a' * 40}/repo/pull/1")


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/Canon-Regularis/Shannon-bot/issues/12",
        "http://github.com/Canon-Regularis/Shannon-bot/issues/12",
        "https://www.github.com/Canon-Regularis/Shannon-bot/issues/12",
        "github.com/Canon-Regularis/Shannon-bot/issues/12",
        "https://github.com/Canon-Regularis/Shannon-bot/issues/12/",
        "https://github.com/Canon-Regularis/Shannon-bot/issues/12#issuecomment-1",
        "https://github.com/Canon-Regularis/Shannon-bot/issues/12?foo=1",
        "  https://github.com/Canon-Regularis/Shannon-bot/issues/12  ",
        "<https://github.com/Canon-Regularis/Shannon-bot/issues/12>",
    ],
)
def test_valid_issue_links_parse(link: str) -> None:
    ref = parse_issue_url(link)

    assert ref.owner == "Canon-Regularis"
    assert ref.name == "Shannon-bot"
    assert ref.number == 12


def test_a_pull_request_link_is_rejected_by_name() -> None:
    with pytest.raises(UnparseableLinkError, match="is a pull request link"):
        parse_issue_url("https://github.com/Canon-Regularis/Shannon-bot/pull/7")


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("", "No link was given"),
        ("https://gitlab.com/owner/repo/issues/12", "is not a github.com link"),
        ("ftp://github.com/owner/repo/issues/12", "is not an http or https link"),
        ("https://github.com/owner", "does not contain an owner and repository"),
        ("https://github.com/owner/repo", "does not point at an issue"),
        ("https://github.com/owner/repo/issues", "does not point at an issue"),
        ("https://github.com/owner/repo/issues/", "does not point at an issue"),
        ("https://github.com/owner/repo/discussions/3", "does not point at an issue"),
        ("https://github.com/owner/repo/issues/abc", "has no valid number"),
        ("https://github.com/owner/repo/issues/0", "has no valid number"),
        ("https://github.com/-bad/repo/issues/1", "is not a valid GitHub owner"),
    ],
)
def test_invalid_issue_links_are_rejected(link: str, message: str) -> None:
    with pytest.raises(UnparseableLinkError, match=message):
        parse_issue_url(link)


def test_the_two_parsers_reject_each_other_symmetrically() -> None:
    """Pasting the wrong kind of link into either command says so rather than failing vaguely."""
    issue_link = "https://github.com/o/r/issues/12"
    pull_link = "https://github.com/o/r/pull/12"

    with pytest.raises(UnparseableLinkError, match="is an issue link, not a pull request link"):
        parse_pull_request_url(issue_link)
    with pytest.raises(UnparseableLinkError, match="is a pull request link, not an issue link"):
        parse_issue_url(pull_link)


class TestNumbersThatAreNotAsciiDigits:
    """str.isdigit is true for a great deal more than 0-9, and int() disagrees with it."""

    def test_an_arabic_indic_number_is_refused_rather_than_converted(self) -> None:
        """It converts silently, so this used to sync a different pull request entirely."""
        with pytest.raises(UnparseableLinkError):
            parse_pull_request_url("https://github.com/o/r/pull/\u0667")

    def test_a_superscript_is_refused_rather_than_raising(self) -> None:
        """isdigit passes it and int() then raises, which escaped as an unhandled error."""
        with pytest.raises(UnparseableLinkError):
            parse_pull_request_url("https://github.com/o/r/pull/\u00b2")

    def test_a_circled_digit_is_refused_rather_than_raising(self) -> None:
        with pytest.raises(UnparseableLinkError):
            parse_issue_url("https://github.com/o/r/issues/\u2467")

    def test_an_ordinary_number_still_works(self) -> None:
        assert parse_pull_request_url("https://github.com/o/r/pull/7").number == 7
