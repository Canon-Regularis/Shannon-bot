from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

import discord

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

EMPTY = "None"
UNKNOWN = "Unknown"

# Discord rejects anything longer than this.
MESSAGE_LIMIT = 2000
TRUNCATED = "\n…"

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


# What each review verdict is called in the thread. GitHub's own words are terse enough that
# spelling them out reads better than echoing the raw state.
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
    return _fit("\n".join(lines))


def _note(
    snapshot: CommentSnapshot | ReviewSnapshot, verb: str, mentions: Mapping[str, int] | None
) -> str:
    """A comment or a review, posted under the metadata block."""
    author = _person(snapshot.author, mentions) if snapshot.author else UNKNOWN

    lines = [f"**{author}** {verb} {_timestamp(snapshot.created_at)}"]
    body = _quote(snapshot.body)
    if body:
        lines.append(body)
    if snapshot.html_url:
        lines.append(f"<{snapshot.html_url}>")
    return _fit("\n".join(lines))


def _ping(lead: str, logins: Iterable[str], mentions: Mapping[str, int] | None) -> str:
    rendered = ", ".join(_person(Actor(login), mentions) for login in logins)
    return f"{lead} {rendered}." if rendered else ""


def _quote(body: str) -> str:
    """A comment body, made safe to drop into a Discord message.

    Blockquoting alone does not stop GitHub markdown rendering: bold, code fences and mentions
    all still resolve inside a quote. So the text is neutralised first, which also means the
    preview can be cut anywhere without leaving a `**` open and bolding everything after it.
    """
    text = (body or "").strip()
    if not text:
        return ""
    if len(text) > COMMENT_PREVIEW_LIMIT:
        text = text[:COMMENT_PREVIEW_LIMIT].rstrip() + "…"
    return "\n".join(f"> {line}" if line else ">" for line in as_plain_text(text).splitlines())


# The one mention form that can still ping somebody. `allowed_mentions` refuses @everyone and
# roles, and `escape_mentions` handles the bare @everyone and @here spellings, but neither
# touches this one, and users are the category the bot is told to honour.
_MENTION = re.compile(r"<(@[!&]?|#)(\d+)>")


def defuse_mentions(text: str) -> str:
    """Stop `<@1234>` resolving; a zero-width space inside the brackets is enough.

    Separate from the markdown escaping because text going into a code span wants this and not
    that, where backslashes would show. Do not assume the span suppresses the ping either:
    `allowed_mentions` gates delivery off the raw content and honours user mentions.
    """
    return _MENTION.sub("<​\\1\\2>", text)


def as_plain_text(text: str) -> str:
    """Render GitHub-authored text so it displays as written.

    Anyone who can comment on the repository reaches into the thread otherwise: `<@1234>` in a
    body resolves to a real ping, and markup that arrives half-finished, or is cut in two by the
    preview limit, restyles everything after it.

    `ignore_links=False` overrides the default. Left on, `escape_markdown` skips whatever its URL
    pattern matches, and that pattern runs to the next space, so a comment ending
    `https://example.com/**` keeps its markers and takes the rest of the message with it. The
    cost: an underscore in a URL comes out escaped and unclickable inside a quoted body, which
    `_note` and the metadata block cover with a link of their own.
    """
    escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(text), ignore_links=False)
    return defuse_mentions(escaped)


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
    rendered = [_code(defuse_mentions(name)) for name in names]
    return ", ".join(rendered) if rendered else EMPTY


def _code(text: str) -> str:
    """Wrap a label in a code span that its own backticks cannot break out of.

    GitHub allows a backtick in a label name. A single-backtick span around one closes early and
    the rest of the line renders as prose. Markdown's own answer is a longer fence.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space keeps a leading or trailing backtick from touching the fence, which would merge
    # with it. Markdown strips one space from each end when rendering.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return UNKNOWN
    # Discord renders this in each reader's own timezone. as_utc because `timestamp()` reads a
    # naive datetime as local time, which would shift every rendered time by the host's offset.
    return f"<t:{int(as_utc(value).timestamp())}:f>"


def _fit(message: str) -> str:
    """Trim to Discord's limit on a line boundary.

    Each line is built balanced, so dropping whole lines leaves what remains rendering properly.
    Cutting at an arbitrary character can land inside `**bold**` or halfway through a `<@123>`
    mention, and the rest of the message goes with it.
    """
    if len(message) <= MESSAGE_LIMIT:
        return message

    budget = MESSAGE_LIMIT - len(TRUNCATED)
    kept: list[str] = []
    used = 0
    for line in message.split("\n"):
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost

    # A single line longer than the whole limit has no boundary to cut on.
    if not kept:
        return message[:budget] + TRUNCATED
    return "\n".join(kept) + TRUNCATED
