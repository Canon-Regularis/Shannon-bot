from __future__ import annotations

import re
from urllib.parse import urlparse

from shannon.domain.errors import UnparseableLinkError
from shannon.domain.models import RepositoryRef
from shannon.domain.text import code_span

GITHUB_HOST = "github.com"

# GitHub's own limits: owners are alphanumeric with single hyphens, repositories also allow
# dots and underscores.
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Both pass _REPO and neither is a name GitHub will issue. The name goes straight into an API
# path signed with the bot's token, and the HTTP client collapses `/repos/{owner}/../pulls/{n}`
# before sending it.
_NOT_REPOSITORIES = frozenset({".", ".."})

_PULL_SEGMENT = "pull"
_ISSUE_SEGMENT = "issues"

# The article is carried along with the noun so the error messages read like English.
_KINDS = {_PULL_SEGMENT: "a pull request", _ISSUE_SEGMENT: "an issue"}


def parse_repository_url(link: str) -> RepositoryRef:
    """Pull owner and repository out of a GitHub link, trimming any deeper path away."""
    owner, repo, _ = _split_repository_path(link)
    return RepositoryRef(owner=owner, name=repo)


def parse_pull_request_url(link: str) -> RepositoryRef:
    """Pull owner, repository and PR number out of a GitHub pull request link.

    Deep links such as `/pull/7/files` and `/pull/7#discussion_r1` are accepted.
    """
    return _parse_object_url(link, _PULL_SEGMENT)


def parse_issue_url(link: str) -> RepositoryRef:
    """Pull owner, repository and issue number out of a GitHub issue link."""
    return _parse_object_url(link, _ISSUE_SEGMENT)


def _parse_object_url(link: str, wanted: str) -> RepositoryRef:
    owner, repo, segments = _split_repository_path(link)

    if len(segments) < 4:
        raise UnparseableLinkError(f"{code_span(link)} does not point at {_KINDS[wanted]}")

    kind = segments[2]
    if kind == wanted:
        return RepositoryRef(owner=owner, name=repo, number=_parse_number(segments[3], link))

    if kind in _KINDS:
        raise UnparseableLinkError(
            f"{code_span(link)} is {_KINDS[kind]} link, not {_KINDS[wanted]} link"
        )
    raise UnparseableLinkError(f"{code_span(link)} does not point at {_KINDS[wanted]}")


def _split_repository_path(link: str) -> tuple[str, str, list[str]]:
    raw = (link or "").strip()
    # Discord wraps links in angle brackets to suppress the embed.
    raw = raw.strip("<>").strip()
    if not raw:
        raise UnparseableLinkError("No link was given")

    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
    except ValueError as exc:
        # An unbalanced square bracket looks like a malformed IPv6 host, and urlparse raises
        # rather than returning anything.
        raise UnparseableLinkError(f"{code_span(link)} is not a usable link") from exc

    if parsed.scheme not in {"http", "https"}:
        raise UnparseableLinkError(f"{code_span(link)} is not an http or https link")

    if host.lower().removeprefix("www.") != GITHUB_HOST:
        raise UnparseableLinkError(f"{code_span(link)} is not a {GITHUB_HOST} link")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:
        raise UnparseableLinkError(f"{code_span(link)} does not contain an owner and repository")

    owner, repo = segments[0], segments[1].removesuffix(".git")
    if not _OWNER.match(owner):
        raise UnparseableLinkError(f"{code_span(owner)} is not a valid GitHub owner")
    if not _REPO.match(repo) or repo in _NOT_REPOSITORIES:
        raise UnparseableLinkError(f"{code_span(repo)} is not a valid GitHub repository name")

    return owner, repo, segments


def _parse_number(segment: str, link: str) -> int:
    """The item number out of a link, or a refusal.

    ASCII digits only. `str.isdigit` is also true of Arabic-Indic digits, which convert silently
    into a number nobody typed, and of superscripts and circled digits, which raise on conversion.
    """
    if not (segment.isascii() and segment.isdigit()):
        raise UnparseableLinkError(f"{code_span(link)} has no valid number")
    number = int(segment)
    if number < 1:
        raise UnparseableLinkError(f"{code_span(link)} has no valid number")
    return number
