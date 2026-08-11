from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from shannon.domain.enums import Priority, Status
from shannon.domain.models import Actor, PullRequestSnapshot

EMPTY = "None"
UNKNOWN = "Unknown"

# Discord rejects anything longer than this.
MESSAGE_LIMIT = 2000


def pull_request_thread_name(snapshot: PullRequestSnapshot) -> str:
    """Thread title for a PR.

    The number goes in front of the PR title so two pull requests that happen to share a title
    stay distinguishable in the channel list.
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


def format_reviewer_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly requested reviewers.

    Anyone without a Discord link is still named, so the thread records who GitHub asked for
    even when nobody has run /link for them.
    """
    rendered = [_person(Actor(login), mentions) for login in logins]
    if not rendered:
        return ""
    return f"Review requested from {', '.join(rendered)}."


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
