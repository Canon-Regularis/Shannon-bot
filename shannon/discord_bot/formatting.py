"""Turning a snapshot into the message Discord shows.

Everything that makes the text itself safe or short lives in `safe_text`; this module decides
what a reader sees and in what order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from shannon.discord_bot.safe_text import (
    EMPTY,
    as_plain_text,
    code_span,
    defuse_mentions,
    fit,
    quote,
)
from shannon.domain.enums import Priority, Status
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    PullRequestSnapshot,
    ReviewSnapshot,
    TrackedSnapshot,
)
from shannon.domain.time import as_utc

UNKNOWN = "Unknown"

_VERDICTS = {
    "approved": "approved this pull request",
    "changes_requested": "requested changes",
    "commented": "left a review",
    "dismissed": "dismissed a review",
}


def thread_name(snapshot: TrackedSnapshot) -> str:
    """Thread title for a tracked item.

    The number goes in front of the title so two items that happen to share a title stay
    distinguishable in the channel list.
    """
    return f"#{snapshot.number} {snapshot.title}".strip()


def format_pull_request(
    snapshot: PullRequestSnapshot,
    *,
    status: Status,
    priority: Priority = Priority.UNSET,
    mentions: Mapping[str, int] | None = None,
) -> str:
    """Render the metadata block that lives at the top of a pull request thread.

    `mentions` maps a lowercased GitHub login to a Discord user ID. Anyone missing from it is
    shown as a plain username, which is the normal case for contributors nobody has linked.
    """
    return _metadata(
        snapshot,
        noun="PR",
        status=status,
        priority=priority,
        mentions=mentions,
        reviewers=snapshot.reviewers,
    )


def format_issue(
    snapshot: IssueSnapshot,
    *,
    status: Status,
    priority: Priority = Priority.UNSET,
    mentions: Mapping[str, int] | None = None,
) -> str:
    """Render the metadata block at the top of an issue thread.

    No reviewers line: GitHub issues have no reviewers, and an always-empty field would be
    noise.
    """
    return _metadata(snapshot, noun="Issue", status=status, priority=priority, mentions=mentions)


def format_reviewer_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly requested reviewers.

    Anyone without a Discord link is still named, so the thread records who GitHub asked for
    even when nobody has run /link for them.
    """
    return _ping("Review requested from", logins, mentions)


def format_assignee_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly assigned people, on the same terms as the reviewer ping."""
    return _ping("Assigned to", logins, mentions)


def format_comment(snapshot: CommentSnapshot, mentions: Mapping[str, int] | None = None) -> str:
    """Render a GitHub comment for its Discord thread."""
    return _note(snapshot, "commented", mentions)


def format_review(snapshot: ReviewSnapshot, mentions: Mapping[str, int] | None = None) -> str:
    """Render a submitted review for its Discord thread.

    A review with an empty body is normal: approving without comment is the common case, and
    the verdict alone is the point.
    """
    return _note(snapshot, _VERDICTS.get(snapshot.verdict, "reviewed"), mentions)


_VERDICTS = {
    "approved": "approved this pull request",
    "changes_requested": "requested changes",
    "commented": "left a review",
    "dismissed": "dismissed a review",
}


def _metadata(
    snapshot: TrackedSnapshot,
    *,
    noun: str,
    status: Status,
    priority: Priority,
    mentions: Mapping[str, int] | None,
    reviewers: Iterable[Actor] | None = None,
) -> str:
    """The block both kinds of item share; only the noun and the reviewers line differ."""
    lines = [
        f"**{noun} Name:** {as_plain_text(snapshot.title) if snapshot.title else UNKNOWN}",
        f"**Type:** {noun}",
        f"**State:** {snapshot.display_state.capitalize()}",
        f"**GitHub Link:** {snapshot.html_url}",
        f"**Author:** {_people([snapshot.author] if snapshot.author else [], mentions)}",
        f"**Assignees:** {_people(snapshot.assignees, mentions)}",
    ]
    if reviewers is not None:
        lines.append(f"**Reviewers:** {_people(reviewers, mentions)}")
    lines += [
        f"**Status:** {status.value}",
        f"**Priority:** {priority.value}",
        f"**Tags:** {_tags(snapshot.label_names)}",
        f"**Last Updated:** {_timestamp(snapshot.updated_at)}",
    ]
    return fit("\n".join(lines))


def _note(
    snapshot: CommentSnapshot | ReviewSnapshot, verb: str, mentions: Mapping[str, int] | None
) -> str:
    """A comment or a review, posted under the metadata block."""
    author = _person(snapshot.author, mentions) if snapshot.author else UNKNOWN

    lines = [f"**{author}** {verb} {_timestamp(snapshot.created_at)}"]
    body = quote(snapshot.body)
    if body:
        lines.append(body)
    if snapshot.html_url:
        lines.append(f"<{snapshot.html_url}>")
    return fit("\n".join(lines))


def _ping(lead: str, logins: Iterable[str], mentions: Mapping[str, int] | None) -> str:
    rendered = ", ".join(_person(Actor(login), mentions) for login in logins)
    return f"{lead} {rendered}." if rendered else ""


def _people(actors: Iterable[Actor], mentions: Mapping[str, int] | None) -> str:
    resolved = [_person(actor, mentions) for actor in actors]
    return ", ".join(resolved) if resolved else EMPTY


def _person(actor: Actor, mentions: Mapping[str, int] | None) -> str:
    discord_user_id = (mentions or {}).get(actor.login.lower())
    return f"<@{discord_user_id}>" if discord_user_id else actor.login


def _tags(names: Iterable[str]) -> str:
    """Label names, which are GitHub-authored text like any other.

    Defused before fencing, not left to the code span. Every other untrusted field in this
    module goes through `as_plain_text`; this was the one that did not, and a label is named by
    anybody with triage rights on the repository.
    """
    rendered = [code_span(defuse_mentions(name)) for name in names]
    return ", ".join(rendered) if rendered else EMPTY


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return UNKNOWN
    # Discord renders this in each reader's own timezone. as_utc because `timestamp()` reads a
    # naive datetime as local time, which would shift every rendered time by the host's offset.
    return f"<t:{int(as_utc(value).timestamp())}:f>"
