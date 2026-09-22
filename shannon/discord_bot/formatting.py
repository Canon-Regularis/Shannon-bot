"""Turning a snapshot into the message Discord shows.

Everything that makes the text itself safe or short lives in `safe_text`; this module decides
what a reader sees and in what order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from shannon.discord_bot.panels import (
    Accent,
    Block,
    BlockKind,
    Panel,
    PanelImage,
    PanelLink,
)
from shannon.discord_bot.rich_text import as_rich_text
from shannon.discord_bot.safe_text import (
    COMMENT_PREVIEW_LIMIT,
    COMMIT_MESSAGE_LIMIT,
    COMMIT_TITLE_LIMIT,
    EMPTY,
    JOB_NAME_LIMIT,
    JOB_NAME_LIMIT_JOINED,
    as_plain_text,
    clipped,
    clipped_job,
    clipped_path,
    code_span,
    defuse_mentions,
)
from shannon.domain.enums import Priority, StateChange, Status
from shannon.domain.models import (
    Actor,
    CheckReport,
    CheckRun,
    CommentSnapshot,
    Commit,
    CommitStats,
    IssueSnapshot,
    ItemNote,
    LabelMove,
    PullRequestSnapshot,
    ReviewCommentSnapshot,
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
) -> Panel:
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
        accent=_pull_request_accent(snapshot),
        reviewers=snapshot.reviewers,
        teams=snapshot.reviewer_teams,
    )


def _pull_request_accent(snapshot: PullRequestSnapshot) -> Accent:
    """GitHub's own colour for the state this one is in.

    Worked out here rather than inside `_metadata`, which takes the snapshot protocol: that has
    no `draft`, because nothing else in the project has one.

    Draft is grey rather than a paler green. It is the one state that says "not yet", and a
    reader scanning a channel wants it to read as quieter than the open ones beside it.
    """
    if snapshot.merged:
        return Accent.MERGED
    if snapshot.closed:
        return Accent.CLOSED
    return Accent.DRAFT if snapshot.draft else Accent.OPEN


def format_issue(
    snapshot: IssueSnapshot,
    *,
    status: Status,
    priority: Priority = Priority.UNSET,
    mentions: Mapping[str, int] | None = None,
) -> Panel:
    """Render the metadata block at the top of an issue thread.

    No reviewers line: GitHub issues have no reviewers, and an always-empty field would be
    noise.

    A closed issue is red whether or not it was closed as completed. GitHub draws "not planned"
    in grey and this snapshot does not carry `state_reason`, so the distinction cannot be made
    here rather than being one somebody decided against.
    """
    return _metadata(
        snapshot,
        noun="Issue",
        status=status,
        priority=priority,
        mentions=mentions,
        accent=Accent.CLOSED if snapshot.closed else Accent.OPEN,
    )


def format_ticket(snapshot: TicketSnapshot, *, status: Status, **_: object) -> Panel:
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
    # Grey, because a draft on a board has no state of its own to colour by. It is also the
    # only block with no author, so it is the only one that never carries a picture.
    return Panel(
        blocks=(Block(BlockKind.FIELDS, "\n".join(lines)),),
        accent=Accent.DRAFT,
        link=_opens_github(snapshot.html_url),
    )


def format_reviewer_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> Panel:
    """Announce newly requested reviewers.

    Anyone without a Discord link is still named, so the thread records who GitHub asked for
    even when nobody has run /link for them.
    """
    return _ping("Review requested from", logins, mentions)


def format_team_ping(teams: Iterable[str], mentions: Mapping[str, int] | None = None) -> Panel:
    """Announce a review asked of a team, as a role mention where the server has linked one.

    A separate renderer rather than a flag on the reviewer one, because Discord writes the two
    with different syntax and getting it wrong is silent: `<@123>` for a role id resolves to
    nobody and renders as a broken mention rather than as an error.
    """
    rendered = ", ".join(_role(name, mentions) for name in teams)
    if not rendered:
        return Panel()
    return _line(f"Review requested from {rendered}.", Accent.SAID)


def _role(team: str, mentions: Mapping[str, int] | None) -> str:
    """A linked team as a role mention, and an unlinked one as its plain name.

    The same bargain the people renderer makes: somebody nobody has linked is still named, so the
    thread records who GitHub asked for even when the server has not run /link_team for them.
    """
    discord_role_id = (mentions or {}).get(team.lower())
    return f"<@&{discord_role_id}>" if discord_role_id else team


def format_assignee_ping(logins: Iterable[str], mentions: Mapping[str, int] | None = None) -> Panel:
    """Announce newly assigned people, on the same terms as the reviewer ping."""
    return _ping("Assigned to", logins, mentions)


# The first emoji in this project, which until now held nothing outside ASCII but an ellipsis
# and a zero-width space. They are here because these lines are read at a glance in a busy
# channel and the words alone do not carry that far: a colour says how urgent an item is before
# anybody has read which label moved.
_PRIORITY_MARKS = {Priority.HIGH: "🔴", Priority.MEDIUM: "🟠", Priority.LOW: "🟢"}
# The same three levels as a bar down the side of the card, so urgency reads before
# anything has been read at all.
_PRIORITY_ACCENTS = {
    Priority.HIGH: Accent.HIGH,
    Priority.MEDIUM: Accent.MEDIUM,
    Priority.LOW: Accent.LOW,
}
# A priority coming off leaves no level behind, so it has no colour to carry.
_PRIORITY_GONE = "⚪"
_STATUS_MARK = "📋"
_TAG_MARK = "🏷️"


def format_label_change(move: LabelMove) -> Panel:
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
            return _line(f"{_PRIORITY_GONE} **Priority cleared:** {named}", Accent.NEUTRAL)
        # UNSET is the only priority with no mark and the test above excluded it, so this
        # lookup cannot miss.
        return _line(
            f"{_PRIORITY_MARKS[move.priority]} **Priority set:** {named}",
            _PRIORITY_ACCENTS[move.priority],
        )

    if move.status is not None:
        return _line(
            f"{_STATUS_MARK} **Status {'set' if move.added else 'cleared'}:** {named}", Accent.SAID
        )

    return _line(f"{_TAG_MARK} Tag {named} {'added' if move.added else 'removed'}.", Accent.NEUTRAL)


def _line(said: str, accent: Accent) -> Panel:
    """One line, in a card of its own.

    The marks are kept beside the colour rather than replaced by it. A bar is drawn where the
    message is; a mark survives into a notification, a search result and a channel preview, which
    is where several of these lines are actually read.
    """
    return Panel(blocks=(Block(BlockKind.HEADING, said),), accent=accent)


# Discord renders `###` as a heading and `-#` as small grey subtext in the content of an ordinary
# message, which is what these are. A heading because a thread closing is the one event in it
# worth finding by scrolling, and the tag lines above are deliberately quieter than this.
_STATE_ACCENTS = {
    StateChange.CLOSED: Accent.CLOSED,
    StateChange.MERGED: Accent.MERGED,
    StateChange.REOPENED: Accent.OPEN,
}
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
_SHUT = "-# This thread is locked and archived."
_SHUT_UNTIL_REOPENED = (
    "-# This thread is locked and archived. Reopen the item on GitHub to reopen it here."
)
_OPEN_AGAIN = "-# This thread is open again."
# Said in the thread rather than left in a log, because the log is read by nobody and the person
# who just closed the item is looking at this. It names the permission because that is the whole
# of the fix, and Discord's own refusal names nothing.
_WOULD_NOT_SHUT = "-# This thread could not be closed: the bot needs Manage Threads."

# Left in a thread the item has been moved off, because Discord cannot move a thread between
# channels and the only honest thing to do with the old one is say where its item went.
#
# Neither line claims the thread is locked, and that is deliberate rather than an omission. This
# has to be posted BEFORE the lock, because posting reopens an archived thread and shutting first
# would be undone by the line itself; at the moment these words are written nobody knows whether
# the lock will land. A server without Manage Threads would otherwise be told it cannot reply
# somewhere it can. "Nothing more will be posted here" is true either way, because the row has
# already stopped pointing at this thread.
_MOVED = "-# This item is now mirrored in {}. Nothing more will be posted in this thread."
_MOVING = "-# This item will be mirrored in {} from now on. Nothing more will be posted here."


def format_thread_moved(thread_id: int) -> str:
    """Point the old thread at the one that replaced it.

    `<#id>` renders a thread mention as readily as a channel one, and the thread is where somebody
    reading this wants to be taken, so it names the replacement itself rather than the channel it
    is in.

    No untrusted text reaches here, which is why nothing is escaped: the words are this module's
    and the id is one Discord gave us.
    """
    return _MOVED.format(f"<#{thread_id}>")


def format_thread_moving(channel_id: int) -> str:
    """The same, for an item with no replacement to name yet.

    A board card has no GitHub endpoint to rebuild it from, so its thread is let go of and the
    poller opens the new one on its next pass. Until then the channel is the most this can say,
    and it is enough to stop somebody waiting in a thread nothing will be posted in.
    """
    return _MOVING.format(f"<#{channel_id}>")


def format_state_change(change: StateChange, *, shut: bool, refused: bool = False) -> Panel:
    """Announce an item closing, merging or reopening, and say what became of the thread.

    The same silence the tag line answers, one step louder. Closing an issue rewrites the block
    and shuts the thread, and Discord says nothing about either, so an item could close, end the
    discussion, and leave no trace in the channel at all.

    `shut` is what the thread actually is, read off the row, rather than what this kind of item
    usually does. Both halves of that matter. A thread this bot could not shut is one people can
    still reply in, so a line claiming otherwise would be telling them they cannot. And a reopen
    whose unlock Discord refused is stepped over rather than failed, on purpose, so a reopened
    item can reach here in a thread that is still shut: promising it is open again is the one
    sentence here that would be a plain lie, in the one case that actually happens.

    `refused` separates the two ways of not being shut, which used to be one. A pull request
    nobody has finished is not shut and there is nothing to say about that. A thread Discord
    would not let this bot shut is not shut either, and saying nothing leaves somebody looking
    at a closed item in a live thread with no idea why. The refusal is why this reaches here at
    all: the delivery used to be dropped on it, taking the whole announcement with it.

    No untrusted text reaches this, which is why nothing is escaped and nothing is defused. The
    words are all this module's own and the three headings are constants. Do not add `fit` for
    symmetry with the block either; two short lines cannot approach the limit.
    """
    heading = _STATE_HEADINGS[change]
    accent = _STATE_ACCENTS[change]

    if change is StateChange.REOPENED:
        if shut:
            return _headed(heading, "", accent)
        return _headed(heading, _OPEN_AGAIN, accent)

    if not shut:
        return _headed(heading, _WOULD_NOT_SHUT if refused else "", accent)
    if change is StateChange.MERGED:
        return _headed(heading, _SHUT, accent)
    return _headed(heading, _SHUT_UNTIL_REOPENED, accent)


def _headed(heading: str, under: str, accent: Accent) -> Panel:
    """A heading and the small line under it, where there is one."""
    blocks = [Block(BlockKind.HEADING, heading)]
    if under:
        blocks.append(Block(BlockKind.SUBHEADING, under))
    return Panel(blocks=tuple(blocks), accent=accent)


# Issue #132. A heading, like the state changes above and for the same reason: leaving draft is
# the moment a pull request starts asking for somebody's time, and that is worth finding by
# scrolling. Green because it is the colour the card turns in the same breath, and the two
# disagreeing would be the reader's problem rather than this module's.
_READY_HEADING = "### 🟢 Ready for review"


def format_ready_for_review(
    initiator: Actor | None,
    *,
    people: Sequence[Actor] = (),
    teams: Sequence[Actor] = (),
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """A pull request taken out of draft, naming who did it and ringing who it now waits on.

    The people go in the block directly under the heading, for the reason the check results give
    at more length: a panel over budget drops blocks from the end, and an allow-list only PERMITS
    a notification while the `<@id>` text is what delivers one.

    The sentence is said whether or not anybody is named, the same bargain the check line makes.
    A pull request with nobody on it still left draft, and a line reading only that is what that
    looks like.

    The initiator is the only untrusted text here and goes through `as_plain_text` like every
    other login; the rest is this module's own words.
    """
    named = " ".join(
        [
            *(_person(person, mentions) for person in people),
            *(_role(team.login, roles) for team in teams),
        ]
    )
    said = f"**{_account(initiator)}** marked this pull request ready for review."
    return _headed(_READY_HEADING, f"{named} {said}".strip(), Accent.OPEN)


def format_comment(
    snapshot: ItemNote,
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """Render a GitHub comment for its Discord thread."""
    assert isinstance(snapshot, CommentSnapshot)
    return _note(snapshot, "commented", mentions, roles)


def format_review(
    snapshot: ItemNote,
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """Render a submitted review for its Discord thread.

    A review with an empty body is normal: approving without comment is the common case, and
    the verdict alone is the point.
    """
    assert isinstance(snapshot, ReviewSnapshot)
    return _note(snapshot, _VERDICTS.get(snapshot.verdict, "reviewed"), mentions, roles)


def format_review_comment(
    snapshot: ItemNote,
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """Render one inline review comment for its Discord thread.

    A message of its own rather than folded into the review carrying it, because GitHub delivers
    the two separately with no promised order and nothing here may wait on a delivery that might
    never come.

    The diff hunk is left out on purpose. It is untrusted repository content several lines long,
    and `fit` drops lines from the end, so a hunk would be the first thing cut and would take the
    link back to GitHub down with it.
    """
    assert isinstance(snapshot, ReviewCommentSnapshot)
    verb = "replied" if snapshot.in_reply_to_id is not None else "commented"
    where = _where(snapshot)
    return _note(snapshot, f"{verb} on {where}" if where else verb, mentions, roles)


def _where(snapshot: ReviewCommentSnapshot) -> str:
    """Which file and line, as the comment itself reports them.

    Defused before fencing, for the reason `_tags` gives below: a code span stops markdown reading
    a name, not Discord reading a mention, and a path is repository content that may be called
    anything somebody can commit.

    `line` is asked before `start_line`, which is not a matter of taste. GitHub never sends a
    start without an end, so asking the other way round would leave a branch nothing can reach.
    """
    if not snapshot.path:
        return ""

    named = code_span(defuse_mentions(clipped_path(snapshot.path)))
    if snapshot.line is None:
        if snapshot.original_line is None:
            # A comment on the file rather than on any line in it.
            return named
        # The diff has moved out from under it, so the only line it still knows is where it was
        # written. Said out loud, because a number quietly pointing somewhere else is worse than
        # no number at all.
        return f"{named} L{snapshot.original_line} (outdated)"
    if snapshot.start_line is None:
        return f"{named} L{snapshot.line}"
    return f"{named} L{snapshot.start_line}-{snapshot.line}"


def _metadata(
    snapshot: TrackedSnapshot,
    *,
    noun: str,
    status: Status,
    priority: Priority,
    mentions: Mapping[str, int] | None,
    accent: Accent,
    reviewers: Iterable[Actor] | None = None,
    teams: Iterable[Actor] = (),
) -> Panel:
    """The block both kinds of item share; only the noun and the reviewers line differ.

    The eleven rows are ONE block rather than one each. Discord allows forty components in a
    view and a row apiece would spend a quarter of them on a single message, and the rows are
    read as a table anyway: a rule between every two of them is worse than none at all.
    """
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
    # Not `fit`, which cut this to a MESSAGE and is the wrong budget twice over: a card
    # holds twice a message, and the description under this block is what should give way
    # first. `Panel.trimmed` does both, at the send, over the blocks it can see.
    blocks = [Block(BlockKind.FIELDS, "\n".join(lines))]
    described = as_rich_text(snapshot.body)
    blocks += _the_description(described.text)
    return Panel(
        blocks=tuple(blocks),
        accent=accent,
        thumbnail_url=snapshot.author.avatar_url if snapshot.author else None,
        link=_opens_github(snapshot.html_url),
        images=_pictures(snapshot, described.images),
    )


def _pictures(snapshot: TrackedSnapshot, lifted: tuple[PanelImage, ...]) -> tuple[PanelImage, ...]:
    """The pictures a card may actually show.

    Public repositories only, and that is about what Discord does rather than about privacy. A
    private repository's images sit behind a short-lived signed URL, and Discord fetches a
    gallery's media itself before it will accept the message at all, so those are guaranteed
    failures. A failure there does not cost the card a picture, it costs the item its whole block,
    which is the same reasoning `mapping._avatar` records about an avatar.

    `None` shows none either. It means GitHub did not say, and not saying is not evidence.
    """
    return lifted if snapshot.repository.private is False else ()


def _the_description(described: str) -> list[Block]:
    """The description under the fields, where there is one.

    Last, because it is the one part of the block that is prose rather than a field, and because
    everything above it is what a reader scanning a channel is looking for. Being last is also
    what makes it the first thing a panel over budget drops, and the whole block goes with its
    label, which is what the old whole-or-nothing rule was written to arrange by arithmetic.

    Asked about the rendered text and never about the body, which is the same trap `_title`
    fell into: a body of nothing but whitespace is truthy, and so is one of nothing but markdown
    markers, and either would put a `**Description:**` label over nothing at all and read as the
    bot having broken.

    Unquoted since issue #113. What separates it from the fields is the rule above it, which is
    what the `> ` markers were doing badly.

    Handed text that has already been through `rich_text` since issue #125, rather than reaching
    for the escaping every other renderer here uses. This is the one block in the project that
    keeps the formatting it was written with, and the one place a picture can come from.
    """
    if not described:
        return []
    return [Block(BlockKind.BODY, f"**Description:**\n{described}")]


def _opens_github(url: str) -> PanelLink | None:
    """The button under the block, for a URL Discord will accept.

    Guarded for the same reason the avatar is: Discord refuses the WHOLE message over a link it
    cannot parse, so an item whose URL arrived malformed would have no block at all rather than
    a block with no button. The `**GitHub Link:**` row carries the address either way.
    """
    return PanelLink(OPEN_ON_GITHUB, url) if url.startswith("https://") else None


def _note(
    snapshot: CommentSnapshot | ReviewSnapshot | ReviewCommentSnapshot,
    verb: str,
    mentions: Mapping[str, int] | None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
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

    said = f"**{author}** {verb} {_timestamp(snapshot.created_at)}"
    blocks = [Block(BlockKind.HEADING, said)]
    # Unquoted since issue #113: the rule above it separates the comment from the line
    # naming its author, which is what the `> ` markers were there to do.
    body = _named_in(clipped(snapshot.body, limit=COMMENT_PREVIEW_LIMIT), mentions, roles)
    if body:
        blocks.append(Block(BlockKind.BODY, body))
    if snapshot.html_url:
        blocks.append(Block(BlockKind.FOOTNOTE, f"<{snapshot.html_url}>"))
    return Panel(blocks=tuple(blocks), accent=Accent.SAID)


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


def _ping(lead: str, logins: Iterable[str], mentions: Mapping[str, int] | None) -> Panel:
    """A ping, or a panel with nothing in it where there is nobody to name.

    An empty panel rather than an empty string, which is the same answer in the new vocabulary:
    callers ask whether there are blocks exactly where they used to ask whether the string was
    empty, and a panel with no blocks is a message Discord would refuse to send.
    """
    rendered = ", ".join(_person(Actor(login), mentions) for login in logins)
    if not rendered:
        return Panel()
    return _line(f"{lead} {rendered}.", Accent.SAID)


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


_COMMIT_MARK = "📝"
_FORCE_PUSH_MARK = "🔁"

# Issue #112. A heading, like the state changes above and for the same reason: a broken build is
# something somebody scrolls a thread looking for.
_CHECKS_PASSED = "### ✅"
_CHECKS_FAILED = "### ❌"

# How many failures are listed one per line, and how many names are joined onto the success line.
# Both exist because `fit` decides what to drop when a message is too long, and what it drops is
# whatever sorted last. A matrix build of fifty jobs would otherwise let that decide which
# failures a reader gets to see.
# This project's own words rather than GitHub's. A button label is one of the few
# places untrusted text could not be escaped into being safe, so none goes there.
OPEN_ON_GITHUB = "Open on GitHub"

JOBS_LISTED = 8
JOBS_NAMED = 15


def format_check_results(
    report: CheckReport,
    *,
    people: Sequence[Actor] = (),
    teams: Sequence[Actor] = (),
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """What CI made of a commit, and whoever is being rung about it.

    The order of these blocks is the design rather than taste. A panel over budget drops blocks
    from the end, and an allow-list only PERMITS a notification: the `<@id>` text is what
    delivers one. So the people go second, above every list, because a panel trimmed down to its
    heading must still ring the people it was sent to ring.

    Failures carry their link and successes do not. A link line runs about a hundred and eighty
    characters, so thirty successes would be five thousand against a budget of two thousand, and
    the thing `fit` threw away to make room would be the failures.
    """
    broken, succeeded, other = report.broken, report.succeeded, report.other
    mark = _CHECKS_FAILED if broken else _CHECKS_PASSED
    jobs, have = ("job", "has") if report.total == 1 else ("jobs", "have")
    written = [
        (
            BlockKind.HEADING,
            f"{mark} {len(succeeded)} / {report.total} {jobs} {have} succeeded.",
        ),
        (BlockKind.SUBHEADING, _told(broken, people, teams, mentions, roles)),
        (BlockKind.FIELDS, _broken_jobs(broken)),
        (BlockKind.FIELDS, _named_jobs("Successful Jobs", succeeded)),
        (BlockKind.FOOTNOTE, _sat_out(other)),
    ]
    return Panel(
        blocks=tuple(Block(kind, said) for kind, said in written if said),
        accent=Accent.FAILED if broken else Accent.PASSED,
    )


def _told(
    broken: Sequence[CheckRun],
    people: Sequence[Actor],
    teams: Sequence[Actor],
    mentions: Mapping[str, int] | None,
    roles: Mapping[str, int] | None,
) -> str:
    """Who is being rung, and what about.

    The sentence is said whether or not anybody is named, because a draft pull request reports its
    results and rings nobody, and a line reading only the verdict is what that looks like.
    """
    named = " ".join(
        [
            *(_person(person, mentions) for person in people),
            *(_role(team.login, roles) for team in teams),
        ]
    )
    said = "One or more jobs did not pass." if broken else "Everything that ran passed."
    return f"{named} {said}".strip()


def _broken_jobs(broken: Sequence[CheckRun]) -> str:
    """The failures, one per line with a link to the log, which is the point of the message."""
    if not broken:
        return ""
    lines = ["**Unsuccessful Jobs:**"]
    lines.extend(_job_line(run) for run in broken[:JOBS_LISTED])
    left = len(broken) - JOBS_LISTED
    if left > 0:
        lines.append(f"-# and {left} more that did not pass.")
    return "\n".join(lines)


def _job_line(run: CheckRun) -> str:
    """One failure. Clipped, defused, fenced, then a bare link.

    Never `[name](url)`. `as_plain_text` breaks `](` apart on purpose so that GitHub-authored text
    cannot build a link, and a job name carrying a `]` would close the label early and leave the
    rest of the line rendering as whatever came next.
    """
    named = code_span(defuse_mentions(clipped_job(run.name, limit=JOB_NAME_LIMIT)))
    return f"- {named} <{run.html_url}>" if run.html_url else f"- {named}"


def _named_jobs(lead: str, runs: Sequence[CheckRun]) -> str:
    """A list of job names on one line, which either survives `fit` whole or goes whole."""
    if not runs:
        return ""
    named = ", ".join(
        code_span(defuse_mentions(clipped_job(run.name, limit=JOB_NAME_LIMIT_JOINED)))
        for run in runs[:JOBS_NAMED]
    )
    left = len(runs) - JOBS_NAMED
    return f"**{lead}:** {named}" + (f", and {left} more" if left > 0 else "")


def _sat_out(other: Sequence[CheckRun]) -> str:
    """The jobs that neither worked nor broke, counted rather than named.

    Subtext, and last, so it is the first thing `fit` sheds. Mostly it is a job a path filter
    skipped, which is worth knowing the shape of and not worth a line each.
    """
    if not other:
        return ""
    jobs = "job" if len(other) == 1 else "jobs"
    return f"-# {len(other)} other {jobs} neither passed nor failed."


def format_commit(commit: Commit) -> Panel:
    """One commit that landed on a pull request, as its own message in the thread.

    **No mentions argument, and that is the requirement rather than an oversight.** `_person` is
    the only thing in this module that builds a `<@id>`, and it needs a mapping to look an account
    up in, so a renderer holding nothing to look one up in cannot ping anybody however it is
    called. A push of ten commits would otherwise be ten notifications about work whoever cares is
    already watching, which is what issue #67 asked not to happen.

    The name is the GitHub account and never `commit.author.name`. That field is free text out of
    `git config user.name`, so anybody who can push could put a colleague's name against their own
    commit; the account is resolved by GitHub from the address and cannot be typed.

    Three lines at most, in the order somebody scanning a thread reads them: who and what, then
    why, then how much. The body is left out rather than rendered blank when the commit has none,
    which is most of them.
    """
    said = f"{_COMMIT_MARK} **{_account(commit.author)}** has committed {_subject(commit)}"
    # Unquoted since issue #113. The rule above it does the separating the `> ` markers were
    # doing, and doing badly.
    body = clipped(commit.description, limit=COMMIT_MESSAGE_LIMIT)

    blocks = [Block(BlockKind.HEADING, said)]
    if body:
        blocks.append(Block(BlockKind.BODY, body))
    blocks.append(Block(BlockKind.FOOTNOTE, _changes(commit.stats)))
    return Panel(blocks=tuple(blocks), accent=Accent.NEUTRAL)


def format_force_push(pusher: Actor | None) -> Panel:
    """Said once when a branch was rewritten, instead of the commits it now holds.

    The commits after a rewrite have new SHAs and would all be announced as new work, which is
    both wrong and noisy: a rebase of five commits says five things nobody did just now. Naming
    what happened is the honest version of that, and it also covers the rollback, where GitHub
    reports nothing ahead at all and silence would be the alternative.
    """
    return _line(
        f"{_FORCE_PUSH_MARK} **{_account(pusher)}** force-pushed this branch, so the commits it "
        "replaced are not announced.",
        Accent.NEUTRAL,
    )


def format_commits_left(count: int) -> Panel:
    """The tail of a push that was not announced line by line.

    Small text, because it is a footnote about what is missing rather than a thing that happened.
    Deliberately not saying whether the cap or a skip left them out: both mean the same thing to
    whoever is reading, which is that GitHub has the rest.
    """
    were = "commit in this push was" if count == 1 else "commits in this push were"
    # Plain, and that is the decision. A bar down the side would make a footnote about what was
    # left out louder than the commits it is a footnote to.
    return Panel.of_text(f"-# {count} earlier {were} not announced.")


def _account(actor: Actor | None) -> str:
    """The account that wrote a commit, or the word for not knowing.

    GitHub answers with no account whenever the committing address is registered to nobody, which
    happens on ordinary work rather than only on anything suspect. Escaped like every other field
    GitHub authored: no login it issues today holds a markdown character, and this module escapes
    everything else it did not write, so the exception would be the thing to explain.
    """
    return as_plain_text(actor.login) if actor is not None else UNKNOWN


def _subject(commit: Commit) -> str:
    """The commit's first line, or its short SHA when it has no message at all.

    `git commit --allow-empty-message` is legal and the mapping layer lets one through, because
    the SHA is the part that had to be there. Without the fallback the line would end on the word
    "committed" and read as the bot having broken rather than as a commit nobody described.
    """
    return clipped(commit.title, limit=COMMIT_TITLE_LIMIT) or code_span(commit.sha[:7])


def _changes(stats: CommitStats) -> str:
    """How much the commit changed, in the shape `git` itself uses.

    Small text, under the thing it is about. One file is one file: a count that reads "1 files"
    is the sort of detail that makes everything above it look unmaintained.
    """
    files = "file" if stats.changed_files == 1 else "files"
    return (
        f"-# With changes: +{stats.additions}, -{stats.deletions}, "
        f"{stats.changed_files} {files} changed"
    )
