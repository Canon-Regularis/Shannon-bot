from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from shannon.domain.enums import Priority, Status
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    PullRequestSnapshot,
    ReviewSnapshot,
    TrackedSnapshot,
)

EMPTY = "None"
UNKNOWN = "Unknown"

# Discord rejects anything longer than this.
MESSAGE_LIMIT = 2000

# A comment is a pointer to the discussion on GitHub, not a copy of it.
COMMENT_PREVIEW_LIMIT = 700


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
    """Render the metadata block that lives at the top of a PR thread.

    `mentions` maps a lowercased GitHub login to a Discord user ID. Anyone missing from it is
    shown as a plain username, which is the normal case for contributors nobody has linked.
    """
    lines = [
        f"**PR Name:** {snapshot.title or UNKNOWN}",
        "**Type:** PR",
        f"**State:** {snapshot.display_state.capitalize()}",
        f"**GitHub Link:** {snapshot.html_url}",
        f"**Author:** {_people([snapshot.author] if snapshot.author else [], mentions)}",
        f"**Assignees:** {_people(snapshot.assignees, mentions)}",
        f"**Reviewers:** {_people(snapshot.reviewers, mentions)}",
        f"**Status:** {status.value}",
        f"**Priority:** {priority.value}",
        f"**Tags:** {_tags(snapshot.label_names)}",
        f"**Last Updated:** {_timestamp(snapshot.updated_at)}",
    ]
    return _fit("\n".join(lines))


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
    lines = [
        f"**Issue Name:** {snapshot.title or UNKNOWN}",
        "**Type:** Issue",
        f"**State:** {snapshot.display_state.capitalize()}",
        f"**GitHub Link:** {snapshot.html_url}",
        f"**Author:** {_people([snapshot.author] if snapshot.author else [], mentions)}",
        f"**Assignees:** {_people(snapshot.assignees, mentions)}",
        f"**Status:** {status.value}",
        f"**Priority:** {priority.value}",
        f"**Tags:** {_tags(snapshot.label_names)}",
        f"**Last Updated:** {_timestamp(snapshot.updated_at)}",
    ]
    return _fit("\n".join(lines))


def format_reviewer_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly requested reviewers.

    Anyone without a Discord link is still named, so the thread records who GitHub asked for
    even when nobody has run /link for them.
    """
    rendered = _mentions_or_names(logins, mentions)
    if not rendered:
        return ""
    return f"Review requested from {rendered}."


def format_assignee_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly assigned people, on the same terms as the reviewer ping."""
    rendered = _mentions_or_names(logins, mentions)
    if not rendered:
        return ""
    return f"Assigned to {rendered}."


def format_comment(snapshot: CommentSnapshot, mentions: Mapping[str, int] | None = None) -> str:
    """Render a GitHub comment for its Discord thread.

    The body is quoted rather than inlined so that GitHub markdown cannot restyle the thread,
    and it is truncated because Discord messages are capped and a comment is not.
    """
    author = _person(snapshot.author, mentions) if snapshot.author else UNKNOWN
    when = _timestamp(snapshot.created_at)

    lines = [f"**{author}** commented {when}"]
    body = _quote(snapshot.body)
    if body:
        lines.append(body)
    if snapshot.html_url:
        lines.append(f"<{snapshot.html_url}>")
    return _fit("\n".join(lines))


# What each review verdict is called in the thread. GitHub's own words are terse enough that
# spelling them out reads better than echoing the raw state.
_VERDICTS = {
    "approved": "approved this pull request",
    "changes_requested": "requested changes",
    "commented": "left a review",
    "dismissed": "dismissed a review",
}


def format_review(snapshot: ReviewSnapshot, mentions: Mapping[str, int] | None = None) -> str:
    """Render a submitted review for its Discord thread.

    A review with an empty body is normal: approving without comment is the common case, and
    the verdict alone is the point.
    """
    author = _person(snapshot.author, mentions) if snapshot.author else UNKNOWN
    verdict = _VERDICTS.get(snapshot.verdict, "reviewed")
    when = _timestamp(snapshot.created_at)

    lines = [f"**{author}** {verdict} {when}"]
    body = _quote(snapshot.body)
    if body:
        lines.append(body)
    if snapshot.html_url:
        lines.append(f"<{snapshot.html_url}>")
    return _fit("\n".join(lines))


def _quote(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    if len(text) > COMMENT_PREVIEW_LIMIT:
        text = text[:COMMENT_PREVIEW_LIMIT].rstrip() + "…"
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _mentions_or_names(logins: Iterable[str], mentions: Mapping[str, int] | None) -> str:
    return ", ".join(_person(Actor(login), mentions) for login in logins)


def _people(actors: Iterable[Actor], mentions: Mapping[str, int] | None) -> str:
    resolved = [_person(actor, mentions) for actor in actors]
    return ", ".join(resolved) if resolved else EMPTY


def _person(actor: Actor, mentions: Mapping[str, int] | None) -> str:
    discord_user_id = (mentions or {}).get(actor.login.lower())
    return f"<@{discord_user_id}>" if discord_user_id else actor.login


def _tags(names: Iterable[str]) -> str:
    rendered = [f"`{name}`" for name in names]
    return ", ".join(rendered) if rendered else EMPTY


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return UNKNOWN
    # Discord renders this in each reader's own timezone.
    return f"<t:{int(value.timestamp())}:f>"


def _fit(message: str) -> str:
    if len(message) <= MESSAGE_LIMIT:
        return message
    return message[: MESSAGE_LIMIT - 1] + "…"
