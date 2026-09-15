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
from shannon.domain.enums import Priority, StateChange, Status
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    LabelMove,
    PullRequestSnapshot,
    ReviewSnapshot,
    TicketSnapshot,
    TrackedSnapshot,
)
from shannon.domain.time import as_utc
from shannon.github.mentions import rewrite

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


def _title(snapshot: TrackedSnapshot) -> str:
    """The item's title as something to show, or the word for having none.

    Stripped before it is judged rather than after. A title of nothing but spaces is truthy, so
    a check asking whether there is a title answered yes and the block rendered a label with
    nothing after it, which reads as the bot having broken rather than as an item nobody named.

    The other two renderers of the same title already agreed on this and the block did not:
    `thread_name` above strips, and `TicketPolicy.thread_name` calls an untitled card an
    untitled card. A draft with a title of spaces opened a thread named "Untitled ticket" whose
    first line named it nothing at all.

    The mapping layer refuses an item whose title is missing or empty, so what reaches here is
    always a string, and whitespace is the one shape of it that carries no title.
    """
    title = snapshot.title.strip()
    return as_plain_text(title) if title else UNKNOWN


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
        teams=snapshot.reviewer_teams,
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


def format_ticket(snapshot: TicketSnapshot, *, status: Status, **_: object) -> str:
    """Render the block at the top of a ticket's thread.

    Three lines, which is what the requirements ask for and all a draft item has to say. The
    other blocks carry an author, assignees and tags; a draft on a board has none of those, and
    a row of empty fields would read as data missing rather than data absent.

    `priority` and `mentions` are accepted and ignored, because the policies all render through
    one signature and a ticket has nobody to mention.
    """
    lines = [
        f"**Ticket Name:** {_title(snapshot)}",
        f"**GitHub Link:** {snapshot.html_url}",
        f"**Status:** {status.value}",
    ]
    return fit("\n".join(lines))


def format_reviewer_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly requested reviewers.

    Anyone without a Discord link is still named, so the thread records who GitHub asked for
    even when nobody has run /link for them.
    """
    return _ping("Review requested from", logins, mentions)


def format_team_ping(teams: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce a review asked of a team, as a role mention where the server has linked one.

    A separate renderer rather than a flag on the reviewer one, because Discord writes the two
    with different syntax and getting it wrong is silent: `<@123>` for a role id resolves to
    nobody and renders as a broken mention rather than as an error.
    """
    rendered = ", ".join(_role(name, mentions) for name in teams)
    return f"Review requested from {rendered}." if rendered else ""


def _role(team: str, mentions: Mapping[str, int] | None) -> str:
    """A linked team as a role mention, and an unlinked one as its plain name.

    The same bargain the people renderer makes: somebody nobody has linked is still named, so the
    thread records who GitHub asked for even when the server has not run /link_team for them.
    """
    discord_role_id = (mentions or {}).get(team.lower())
    return f"<@&{discord_role_id}>" if discord_role_id else team


def format_assignee_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> str:
    """Announce newly assigned people, on the same terms as the reviewer ping."""
    return _ping("Assigned to", logins, mentions)


# The first emoji in this project, which until now held nothing outside ASCII but an ellipsis
# and a zero-width space. They are here because these lines are read at a glance in a busy
# channel and the words alone do not carry that far: a colour says how urgent an item is before
# anybody has read which label moved.
_PRIORITY_MARKS = {Priority.HIGH: "🔴", Priority.MEDIUM: "🟠", Priority.LOW: "🟢"}
# A priority coming off leaves no level behind, so it has no colour to carry.
_PRIORITY_GONE = "⚪"
_STATUS_MARK = "📋"
_TAG_MARK = "🏷️"


def format_label_change(move: LabelMove) -> str:
    """Announce one label going on or coming off, in the words its group calls for.

    The metadata block above already says which labels an item has, and it is rewritten on every
    delivery, so this says nothing the reader could not scroll up for. It exists because an edit
    to that block is invisible from the channel: Discord posts no message for one, notifies
    nobody, and does not bump the thread, so tagging an item looked from outside like nothing had
    happened at all.

    Three groups rather than one, because two of them are labels this bot writes itself. Status
    and priority both live as labels on the repository, so `/set_done` and somebody tagging an
    issue `bug` arrive down the same webhook, and saying the same sentence about both buried the
    one that matters under the one that does not.

    Which group a label is in was decided where the move was read, so the order of the tests
    below is a formality and not a precedence rule: no status name parses as a priority and no
    priority spelling is one of the five statuses, which a test pins rather than assumes.

    "Tag" rather than "label", to match the word the block uses for the same thing.

    Named in a code span like the block's own tags, and defused first: a label is named by
    anybody with triage rights on the repository, so it is untrusted text like any other. Not
    put through `fit`, and deliberately: GitHub caps a label name at fifty characters and the
    fence and marks add a handful, so this cannot approach the message limit.

    One thing it does not claim. A status label moving does not move the item's stored status:
    nothing on the webhook path reads a status off a label, so the block above may go on saying
    something else. The line reports what somebody did to the labels, which is what every line
    here reports, and the two are both true.
    """
    named = code_span(defuse_mentions(move.name))

    if move.priority is not Priority.UNSET:
        if not move.added:
            return f"{_PRIORITY_GONE} **Priority cleared:** {named}"
        # UNSET is the only priority with no mark and the test above excluded it, so this
        # lookup cannot miss.
        return f"{_PRIORITY_MARKS[move.priority]} **Priority set:** {named}"

    if move.status is not None:
        return f"{_STATUS_MARK} **Status {'set' if move.added else 'cleared'}:** {named}"

    return f"{_TAG_MARK} Tag {named} {'added' if move.added else 'removed'}."


# Discord renders `###` as a heading and `-#` as small grey subtext in the content of an ordinary
# message, which is what these are. A heading because a thread closing is the one event in it
# worth finding by scrolling, and the tag lines above are deliberately quieter than this.
_STATE_HEADINGS = {
    StateChange.CLOSED: "### 🔒 Closed",
    StateChange.MERGED: "### 🟣 Merged",
    StateChange.REOPENED: "### 🔓 Reopened",
}
# Two ways of saying the thread is shut, because only one of them can be undone. A closed issue
# reopens on GitHub and the thread comes back with it; a merged pull request does not reopen at
# all, so pointing somebody at GitHub to undo it would send them looking for a button that is not
# there. This is not a corner: `/set_done` is what locks a pull request and the requirements have
# it run before the merge, so a merged item arriving in a shut thread is the ordinary order.
_LOCKED = "-# This thread is locked."
_LOCKED_UNTIL_REOPENED = "-# This thread is locked. Reopen the item on GitHub to reopen it here."
_OPEN_AGAIN = "-# This thread is open again."


def format_state_change(change: StateChange, *, locked: bool) -> str:
    """Announce an item closing, merging or reopening, and say what became of the thread.

    The same silence the tag line answers, one step louder. Closing an issue rewrites the block
    and locks the thread, and Discord says nothing about either, so an item could close, shut
    the discussion, and leave no trace in the channel at all.

    `locked` is what the thread actually is, read off the row, rather than what this kind of
    item usually does. Both halves of that matter. A pull request closes without its thread
    being shut, so a line claiming otherwise would tell people they cannot reply where they can.
    And a reopen whose unlock Discord refused is stepped over rather than failed, on purpose, so
    a reopened item can reach here in a thread that is still shut: promising it is open again is
    the one sentence here that would be a plain lie, in the one case that actually happens.

    No untrusted text reaches this, which is why nothing is escaped and nothing is defused. The
    words are all this module's own and the three headings are constants. Do not add `fit` for
    symmetry with the block either; two short lines cannot approach the limit.
    """
    heading = _STATE_HEADINGS[change]

    if change is StateChange.REOPENED:
        if locked:
            return heading
        return f"{heading}\n{_OPEN_AGAIN}"

    if not locked:
        return heading
    if change is StateChange.MERGED:
        return f"{heading}\n{_LOCKED}"
    return f"{heading}\n{_LOCKED_UNTIL_REOPENED}"


def format_comment(
    snapshot: CommentSnapshot,
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> str:
    """Render a GitHub comment for its Discord thread."""
    return _note(snapshot, "commented", mentions, roles)


def format_review(
    snapshot: ReviewSnapshot,
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> str:
    """Render a submitted review for its Discord thread.

    A review with an empty body is normal: approving without comment is the common case, and
    the verdict alone is the point.
    """
    return _note(snapshot, _VERDICTS.get(snapshot.verdict, "reviewed"), mentions, roles)


def _metadata(
    snapshot: TrackedSnapshot,
    *,
    noun: str,
    status: Status,
    priority: Priority,
    mentions: Mapping[str, int] | None,
    reviewers: Iterable[Actor] | None = None,
    teams: Iterable[Actor] = (),
) -> str:
    """The block both kinds of item share; only the noun and the reviewers line differ."""
    lines = [
        f"**{noun} Name:** {_title(snapshot)}",
        f"**Type:** {noun}",
        f"**State:** {snapshot.display_state.capitalize()}",
        f"**GitHub Link:** {snapshot.html_url}",
        f"**Author:** {_people([snapshot.author] if snapshot.author else [], mentions)}",
        f"**Assignees:** {_people(snapshot.assignees, mentions)}",
    ]
    if reviewers is not None:
        # Teams named plainly and never looked up in `mentions`, which maps logins to accounts.
        # A slug that happens to match a login is not that person, and rendering one as the other
        # would put somebody's name against a team they have nothing to do with.
        asked = [*(_person(person, mentions) for person in reviewers), *(t.login for t in teams)]
        lines.append(f"**Reviewers:** {', '.join(asked) if asked else EMPTY}")
    lines += [
        f"**Status:** {status.value}",
        f"**Priority:** {priority.value}",
        f"**Tags:** {_tags(snapshot.label_names)}",
        f"**Last Updated:** {_timestamp(snapshot.updated_at)}",
    ]
    return fit("\n".join(lines))


def _note(
    snapshot: CommentSnapshot | ReviewSnapshot,
    verb: str,
    mentions: Mapping[str, int] | None,
    roles: Mapping[str, int] | None = None,
) -> str:
    """A comment or a review, posted under the metadata block.

    `roles` is kept apart from `mentions` rather than folded in with it, because a team slug that
    happens to match a login is not that person. The two are looked up in different tables and
    Discord writes them with different syntax, and a user id written as a role mention resolves
    to nobody and reads as broken rather than as an error.

    The names in the body are swapped in the quoted text and nowhere else. The line above it
    carries a mention this bot built itself, live and never defused, and a GitHub login may be
    all digits, so handing the assembled message to the swap would let `<@7>` be read as a name
    and rewritten into somebody else.
    """
    author = _person(snapshot.author, mentions) if snapshot.author else UNKNOWN

    lines = [f"**{author}** {verb} {_timestamp(snapshot.created_at)}"]
    body = _named_in(quote(snapshot.body), mentions, roles)
    if body:
        lines.append(body)
    if snapshot.html_url:
        lines.append(f"<{snapshot.html_url}>")
    return fit("\n".join(lines))


def _named_in(
    body: str, mentions: Mapping[str, int] | None, roles: Mapping[str, int] | None
) -> str:
    """Turn the names a comment writes into mentions, where this server knows who they are.

    The whole point of the feature: tagging somebody on GitHub reached them on GitHub and reached
    nobody here, which is where the team is actually reading.

    Anybody not linked is left exactly as written, which is the bargain every other renderer in
    this module already makes: the thread records who was named even where the server has no way
    to reach them.
    """
    return rewrite(
        body,
        person=lambda login: _mention(login, mentions, "<@{}>"),
        team=lambda slug: _mention(slug, roles, "<@&{}>"),
    )


def _mention(name: str, known: Mapping[str, int] | None, shape: str) -> str | None:
    """The mention for a name this server has an id for, or None to leave it as written."""
    found = (known or {}).get(name.lower())
    return shape.format(found) if found else None


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
