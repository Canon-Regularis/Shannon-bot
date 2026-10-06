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
from shannon.discord_bot.rich_text import Images, as_note_text, as_rich_text
from shannon.discord_bot.safe_text import (
    CARD_FIELD_LIMIT,
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
from shannon.domain.enums import Priority, StateChange, Status, spoken
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
from shannon.github.mentions import rewrite_a_note

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


def format_ticket(
    snapshot: TicketSnapshot,
    *,
    status: Status,
    mentions: Mapping[str, int] | None = None,
    **_: object,
) -> Panel:
    """Render the block at the top of a ticket's thread.

    Built by `_rows`, the same way the other two blocks are, which is the half of issue #166 about
    duplication: these labels used to be written out a second time here and had already drifted to
    three of them.

    Five rows until issue #182, because the board read asked GitHub for Title and Status alone and
    a card could carry nothing else. It asks for the board's own fields now, so the rows below are
    the ones a draft card can actually fill.

    **A row with nothing in it is still left out**, which is the rule #166 set and the reason this
    stayed additive. The names are the board owner's to change, so a board calling its field
    something else - or a card with that field unset - has no value for the row and the row does
    not appear. That is deliberately not the same as rendering `None`: an always-empty field reads
    as data missing rather than data absent, which is what keeps a reviewers line off an issue.

    `State` is still left out. A board column is not a closed state, so a card in Done is as open as
    one in Todo, and the one thing that ends a card - archiving or deleting it off the board, issue
    #198 - also shuts the thread it would be shown in. The row could only ever say `Open`.

    **A description was documented here as impossible, and that was wrong.** The claim was that a
    board item carries no body text at all. It is true of a card wrapping an issue, whose own body
    reached its thread from its own webhook - and false of a DRAFT, which keeps its text under
    `content.body` and has nowhere else to keep it. Issue #182 read a real draft off a real board
    and found it there. It is shown now, through the same `_the_description` every other block uses,
    so it arrives with the formatting it was written with.

    `mentions` is read now and `priority` still is not, which is a split worth naming. A card
    carries a creator and assignees since issue #182, so the map has somebody to resolve; a card's
    priority is the board's single-select rather than the row's label-derived enum, and it arrives
    on the snapshot - reading the parameter would show whatever the row happened to hold.
    """
    # Grey, because a draft on a board has no state of its own to colour by.
    return Panel(
        blocks=(
            _rows(
                ("Ticket Name", _title(snapshot)),
                ("Type", "Ticket"),
                ("GitHub Link", snapshot.html_url),
                *_if_set(
                    "Creator", _person(snapshot.author, mentions) if snapshot.author else None
                ),
                *_if_set(
                    "Assignees",
                    _people(snapshot.assignees, mentions) if snapshot.assignees else None,
                ),
                ("Status", spoken(status)),
                *_if_set("Priority", snapshot.priority_name),
                *_if_set("Story Point", snapshot.story_point),
                *_if_set("Iteration", snapshot.iteration),
                *_if_set("Area", snapshot.area),
                *_if_set("Tags", _tags(snapshot.label_names) if snapshot.label_names else None),
                *_if_set(
                    "Created", as_timestamp(snapshot.created_at) if snapshot.created_at else None
                ),
                ("Last Updated", as_timestamp(snapshot.updated_at)),
            ),
            # Last, and dropped entirely for a card nobody wrote anything on - the same shape and
            # the same guard the other two blocks use.
            *_the_description(as_rich_text(snapshot.body).text),
        ),
        accent=Accent.DRAFT,
        thumbnail_url=snapshot.author.avatar_url if snapshot.author else None,
        link=_opens_github(snapshot.html_url),
    )


def _if_set(label: str, value: str | None) -> tuple[tuple[str, str], ...]:
    """One row, or none at all where the card has nothing to put in it. Issue #182.

    Splatted into the `_rows` call rather than filtered inside it, and that is a coverage decision
    as much as a style one: a filter in the builder would be a branch every caller with a full set
    of rows could never take, and the floor here is a hundred per cent of branches. A one-line
    conditional expression records no arc at all, so this costs nothing to cover either way.
    """
    return ((label, value),) if value else ()


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
    and priority both live as labels on the repository, so `/status Done` and somebody tagging an
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
# there. This is not a corner: `/status Done` is what locks a pull request and the requirements have
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
# Neither of THESE two lines claims the thread is locked, and that is deliberate rather than an
# omission. Both have to be posted BEFORE the lock, because posting reopens an archived thread and
# shutting first would be undone by the line itself; at the moment these words are written nobody
# knows whether the lock will land. A server without Manage Threads would otherwise be told it
# cannot reply somewhere it can. "Nothing more will be posted here" is true either way, because the
# row has already stopped pointing at this thread.
#
# `_CONVERTED` below used to sit under this rule and no longer does: it is posted after the lock
# rather than before it, so it knows the answer and says it. See `format_card_converted`.
_MOVED = "-# This item is now mirrored in {}. Nothing more will be posted in this thread."
_MOVING = "-# This item will be mirrored in {} from now on. Nothing more will be posted here."

# Issue #184. A converted card's thread is an END, like a closed issue's or a merged pull request's,
# so it gets the same shape those get: a heading worth finding by scrolling, a colour, and a line
# saying what became of the thread.
#
# Purple because purple already means "this went somewhere and is finished" (`MERGED`), and red
# `CLOSED` would read as abandoned - which a conversion is the opposite of. Its own name rather
# than `Accent.MERGED` because the enum asks for one: a converted card is not a merged pull request.
#
# No "Nothing more will be posted here": the lock line below says it, and says it truthfully.
_CONVERTED_HEADING = "### 🟣 Converted"
_CONVERTED = "-# This card became {} on GitHub, which is mirrored in a thread of its own."


def format_card_converted(html_url: str, *, shut: bool) -> Panel:
    """Point a draft card's thread at the issue it has become, and say the thread is finished.

    The ISSUE rather than its thread, deliberately. The issue's own thread is opened by its
    `opened` webhook, which may not have arrived when this is written and may not arrive at
    all if that delivery is still being retried - so naming a thread id here would be a
    guess, while the issue's page exists the moment GitHub converted it.

    `shut` is what the caller has already DONE, not what it intends: the hand-over shuts the
    thread, posts this, and shuts it again, because the post reopens what the shut closed. That
    order is the whole reason this may claim the lock where `_MOVED` and `_MOVING` may not, and
    it is the order `format_state_change` is written for as well.

    Nothing is said when the lock was refused, rather than the `_WOULD_NOT_SHUT` line the state
    changes use. That line names a permission to go and grant, which is worth saying to somebody
    who just ran a command and is looking at the thread - but this is a poller, nobody is
    watching, and the thread pointer is already gone, so granting it would never make this run
    again. A sentence offering a fix that cannot work is no better than the one it replaced.

    No untrusted text: the URL is one GitHub gave us for an item it has just created, and
    the words are this module's. One block rather than two, because a heading takes the single
    subheading after it and anything further would be drawn as a section of its own.
    """
    said = _CONVERTED.format(html_url)
    under = f"{said}\n{_SHUT}" if shut else said
    return _headed(_CONVERTED_HEADING, under, Accent.CONVERTED)


# Issue #198. A draft card taken off the board, put back, or deleted. The same shape a conversion
# gets, for the same reason: a heading worth finding by scrolling, a colour, and a line saying what
# became of the thread - claimed only where the caller already made it so.
#
# Archiving is the one that can be undone, so its lock line says how, the way a closed issue's does;
# a deletion cannot, so it gets the plain `_SHUT` a merge gets.
_ARCHIVED_HEADING = "### 📦 Archived"
_ARCHIVED = "-# This card was archived on the board."
_SHUT_UNTIL_RESTORED = (
    "-# This thread is locked and archived. Restore the card on the board to reopen it here."
)
_RESTORED_HEADING = "### 📤 Restored"
_RESTORED = "-# This card is back on the board."
_DELETED_HEADING = "### 🗑️ Deleted"
_DELETED = "-# This card was deleted from the board."


def format_card_archived(*, shut: bool) -> Panel:
    """Tell a draft card's thread its card was archived. Issue #198.

    `shut` is what the caller already DID, the contract `format_card_converted` has: the poller
    shuts the thread, posts this, and shuts it again, so it knows. Where Discord refused, nothing
    is said about the lock, rather than a promise of one that is not there.
    """
    under = f"{_ARCHIVED}\n{_SHUT_UNTIL_RESTORED}" if shut else _ARCHIVED
    return _headed(_ARCHIVED_HEADING, under, Accent.ARCHIVED)


def format_card_restored(*, reopened: bool) -> Panel:
    """Tell a draft card's thread its card is back on the board. Issue #198.

    `reopened` is whether Discord actually opened the thread again. Where it would not, the thread
    is still shut and saying it is open again would be the one false sentence in it. Anything the
    card gained while it was archived follows in its own line, the ordinary one a moved card gets.
    """
    under = f"{_RESTORED}\n{_OPEN_AGAIN}" if reopened else _RESTORED
    return _headed(_RESTORED_HEADING, under, Accent.OPEN)


def format_card_deleted(*, shut: bool) -> Panel:
    """Tell a draft card's thread its card was deleted, and that the thread is finished. Issue #198.

    The end a conversion, a closed issue and a merged pull request already get: shut, said, shut
    again. `shut` is what the caller already did, as for the other two above.
    """
    under = f"{_DELETED}\n{_SHUT}" if shut else _DELETED
    return _headed(_DELETED_HEADING, under, Accent.DELETED)


# Issue #182. A card's board metadata moved. Blue, like every other line that reports something
# somebody did rather than a state the item reached: a card gaining a story point is news, not a
# verdict, and the greens and reds are spoken for by things that are.
_CARD_CHANGED_HEADING = "### 📋 Ticket updated"


def format_card_changed(moved: Sequence[tuple[str, str, str]]) -> Panel:
    """One line for everything that moved on a card since the last poll.

    ONE line rather than one per field, which is the shape the poll gives it: a board read sees a
    card's fields together, so somebody dragging a card and setting its points in the same minute
    did one thing and hears about it once. Per-field lines would ring three times for one action,
    and the webhook-driven announcers only look granular because a webhook arrives per change.

    Every value here is board-authored - an option somebody named, a login, a label, a title, a
    description - so all of it goes through `clipped`, which cuts and then defuses. A board owner
    is not an attacker, but a label name is repository content and the rule in this module is that
    GitHub-authored text is defused wherever it lands.

    CUT, and not only for tidiness. A description runs to seven hundred characters on its own and
    there are eleven fields carrying two values each, so an uncut line would go past what Discord
    accepts and cost the whole message rather than the extra words. The block directly above
    carries every value in full, which is what makes a short form the right one here.

    A field that was unset reads as the same `None` an empty row reads as, which is deliberate:
    "None to HIGH" is what setting a priority for the first time actually did.
    """
    said = "\n".join(
        f"-# {label}: {clipped(was, limit=CARD_FIELD_LIMIT) if was else EMPTY} → "
        f"{clipped(now, limit=CARD_FIELD_LIMIT) if now else EMPTY}"
        for label, was, now in moved
    )
    return _headed(_CARD_CHANGED_HEADING, said, Accent.SAID)


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


# Issue #140. The other half of the switch, which issue #132 decided to keep quiet. Grey for the
# reason the one above is green: it is the colour the card turns in the same breath, and the two
# disagreeing would be the reader's problem rather than this module's.
#
# A mark nothing else here uses, and one that makes no colour claim of its own, so it neither
# collides with the priority dots nor argues with the bar beside it. It says unfinished, which is
# the whole of what a draft is.
_DRAFTED_HEADING = "### 🚧 Back to draft"


def format_back_to_draft(
    initiator: Actor | None,
    *,
    people: Sequence[Actor] = (),
    teams: Sequence[Actor] = (),
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """A pull request put back into draft, naming who did it and telling who was waiting on it.

    Its own renderer rather than a flag on the one above, for the reason `format_team_ping` gives
    at more length: getting the syntax of a mention wrong is silent, and a flag is the shape that
    lets it happen.

    `roles` is accepted and never read, which is the point of taking it. A team is named here in
    plain text however it is linked, because a role mention rings everybody holding the role and
    Discord gives nobody a way to leave one person out of one: asking a team to look is worth
    that, and telling them to stop looking is not worth waking them for. The caller already hands
    this an empty mapping, so ignoring the parameter is belt and braces — but the failure it
    guards against is invisible when it is written and unmutable when it is read, which is the
    asymmetry that earns the second layer.

    That it takes the argument at all is what lets both halves share one `Renderer`, and one
    protocol is what lets one announcer class serve both.
    """
    named = " ".join(
        [
            *(_person(person, mentions) for person in people),
            *(_role(team.login, None) for team in teams),
        ]
    )
    said = f"**{_account(initiator)}** converted this pull request to draft."
    return _headed(_DRAFTED_HEADING, f"{named} {said}".strip(), Accent.DRAFT)


# Issue #155. A heading, like the state changes and the two draft lines above, and for the same
# reason: the moment a pull request stops waiting on anybody is one somebody scrolls a thread
# looking for.
#
# Not the tick, which is `_CHECKS_PASSED` below. One mark meaning both "CI is green" and "the
# review is finished" is precisely the confusion a reader has to untangle when both land a minute
# apart, which on a healthy pull request is the ordinary case rather than the unlucky one. A
# chequered flag says the run is over, which is the whole of what this means, and like the draft
# mark it makes no colour claim of its own, so it neither collides with the priority dots nor
# argues with the bar beside it.
_APPROVED_HEADING = "### 🏁 Approved"


def format_everyone_approved(
    approvals: int,
    *,
    people: Sequence[Actor] = (),
    teams: Sequence[Actor] = (),
    mentions: Mapping[str, int] | None = None,
    roles: Mapping[str, int] | None = None,
) -> Panel:
    """Every review asked for has come back approving, said to the people who now have to act.

    A count rather than the approvers. Each of them is already named in this thread one message
    above, by the line that mirrored their review; naming them again would say nothing new, and
    it would do it through a live mention map, so the last approver would be rung about their own
    approval.

    The people named are the author and the assignees, for the reason the check-failure line
    names the same pair: they are who a pull request waits on once the reviewing is done. Whoever
    approved last is not dropped the way the draft switch drops whoever pressed the button —
    there the initiator knows what they just did, and here the news is about everybody else's
    reviews rather than their own.

    `teams` and `roles` are accepted and go through `_role` like the ready line's, so this
    matches the shape the other two audience-taking renderers use. The caller hands an empty
    sequence, because the audience is people.

    The sentence is said whether or not anybody is named, the bargain the check line and the
    ready line both make. A pull request whose author's account is gone and which nobody was
    assigned still stopped waiting on its reviewers, and a line reading only that is what that
    looks like.
    """
    named = " ".join(
        [
            *(_person(person, mentions) for person in people),
            *(_role(team.login, roles) for team in teams),
        ]
    )
    reviews = "review" if approvals == 1 else "reviews"
    said = f"{approvals} {reviews}, all of them approving, and nobody is still being waited on."
    return _headed(_APPROVED_HEADING, f"{named} {said}".strip(), Accent.PASSED)


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


def _rows(*rows: tuple[str, str]) -> Block:
    """The fields of a block, as one component. Issue #166.

    One block rather than one each. Discord allows forty components in a view and a row apiece
    would spend a quarter of them on a single message, and the rows are read as a table anyway: a
    rule between every two of them is worse than none at all.

    Shared by every block that has fields, which is what the issue asked for: the label shape was
    written out twice and the second copy was a ticket's, three rows deep and drifting. A row a
    caller has no data for is left out by not being passed, rather than passed empty and filtered
    here - an always-empty field is noise, which is the rule `format_issue` already follows for
    reviewers, and a filter would be a branch nothing could take.
    """
    return Block(BlockKind.FIELDS, "\n".join(f"**{label}:** {value}" for label, value in rows))


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

    The rows go through `_rows`, which a ticket's block shares since issue #166.
    """
    rows = [
        (f"{noun} Name", _title(snapshot)),
        ("Type", noun),
        ("State", snapshot.display_state.capitalize()),
        ("GitHub Link", snapshot.html_url),
        ("Author", _people([snapshot.author] if snapshot.author else [], mentions)),
        ("Assignees", _people(snapshot.assignees, mentions)),
    ]
    if reviewers is not None:
        # Teams named plainly and never looked up in `mentions`, which maps logins to accounts.
        # A slug that happens to match a login is not that person, and rendering one as the other
        # would put somebody's name against a team they have nothing to do with.
        asked = [*(_person(person, mentions) for person in reviewers), *(t.login for t in teams)]
        rows.append(("Reviewers", ", ".join(asked) if asked else EMPTY))
    rows += [
        ("Status", spoken(status)),
        ("Priority", spoken(priority)),
        ("Tags", _tags(snapshot.label_names)),
        ("Last Updated", as_timestamp(snapshot.updated_at)),
    ]
    # Not `fit`, which cut this to a MESSAGE and is the wrong budget twice over: a card
    # holds twice a message, and the description under this block is what should give way
    # first. `Panel.trimmed` does both, at the send, over the blocks it can see.
    blocks = [_rows(*rows)]
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

    said = f"**{author}** {verb} {as_timestamp(snapshot.created_at)}"
    blocks = [Block(BlockKind.HEADING, said)]
    # Unquoted since issue #113: the rule above it separates the comment from the line
    # naming its author, which is what the `> ` markers were there to do.
    # `as_note_text` rather than a recipe spelled out here, because `services.notes` reads the
    # names to look up out of the very same string and the two have to agree to the character.
    body = _named_in(as_note_text(snapshot.body), mentions, roles)
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

    `rewrite_a_note` rather than `rewrite`, so that nothing here chooses whether a name inside a
    code span counts: that answer has to match the one the note path reads with, and it is paired
    with it at the other end rather than passed from both.

    Code spans are skipped, which GitHub does and this could not until issue #166: the escaping
    used to turn the backticks into literal characters before the swap ran, so there was no span
    left to respect and a name inside one resolved. Keeping that once the backticks survive would
    put a `<@id>` inside a code span, where Discord shows it as the text it is - while delivering
    the notification anyway off the raw content. A ping with nothing to see.
    """
    return rewrite_a_note(
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
    """A live mention where this server knows the account, and the plain login where it does not.

    Escaped on the way out, like `_account` and like every other field this module did not write
    itself. No login GitHub issues today holds a markdown character, so this changes nothing that
    can be seen — but the six renderers reading this were the one place in the module where that
    was an assumption about GitHub rather than a rule of this bot's, and the rule is cheaper to
    keep than the assumption is to re-check.
    """
    discord_user_id = (mentions or {}).get(actor.login.lower())
    return f"<@{discord_user_id}>" if discord_user_id else as_plain_text(actor.login)


def _tags(names: Iterable[str]) -> str:
    """Label names, which are GitHub-authored text like any other.

    Defused before fencing, not left to the code span. Every other untrusted field in this
    module goes through `as_plain_text`; this was the one that did not, and a label is named by
    anybody with triage rights on the repository.
    """
    rendered = [code_span(defuse_mentions(name)) for name in names]
    return ", ".join(rendered) if rendered else EMPTY


def as_timestamp(value: datetime | None) -> str:
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
    # Issue #166. The subject above stays escaped - it sits inside a `**`-matched line, and
    # rendering it could leave that line unbalanced - but the description is prose and is shown as
    # it was written. Images become links rather than a gallery: a `Commit` carries no repository,
    # so the private-repo gate a card's pictures go through has nothing to read here.
    body = as_rich_text(commit.description, limit=COMMIT_MESSAGE_LIMIT, images=Images.AS_LINKS).text

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
