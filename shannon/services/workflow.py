"""Moving an item through the workflow: its status and its priority.

GitHub is written first and Discord second, which the requirements ask for and which is also the
only order that can be recovered from. The labels are the record; the stored status and the
metadata block are a mirror of them, so a run that dies half way leaves the item correct on
GitHub and stale here, and the next event or the next command corrects it. The other order
leaves Discord claiming something GitHub never agreed to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.domain.board import board_owner, must_pass_through
from shannon.domain.enums import ObjectType, Priority, Status, spoken
from shannon.domain.errors import ItemNotReadyError, PermanentError, ShannonError
from shannon.domain.models import Fetcher, Label, TrackedSnapshot
from shannon.domain.text import code_span
from shannon.github import labels
from shannon.github.client import GitHubClient
from shannon.github.projects import BoardOrder, CardMove, CardMoved
from shannon.services.labels import RepositoryLabels
from shannon.services.sync.items import ShutsAndKnowsServers, SyncsItems
from shannon.services.sync.one_at_a_time import ItemLock

logger = logging.getLogger(__name__)


class NotAnItemThreadError(ShannonError):
    """The command was run somewhere that is not a tracked item's thread."""


class WorkflowRefusedError(ShannonError):
    """The change is not one this item can be given right now."""


class ItemMovedError(ShannonError):
    """Somebody else moved the item while this was waiting its turn, so nothing was touched.

    Deliberately not a `WorkflowRefusedError`, which the board poller reads as a final answer
    and writes the card off for: the column is recorded, and since nothing else rederives a
    status from a board, no poll looks at that card again. This is the opposite of final. The
    only thing that brings the card round is the column being left unwritten, which is what the
    poller does for any other `ShannonError`.
    """


@dataclass(frozen=True, slots=True)
class ItemKind:
    """How to read and re-render one kind of item.

    Both halves differ by object type and neither belongs here: fetching is the client's, and
    rendering is the sync service's. The command cannot pick between them because it only knows
    which thread it is in, so the picking happens here.
    """

    fetch: Fetcher
    sync: SyncsItems


@dataclass(frozen=True, slots=True)
class _Lock:
    """What became of the one Discord call a status change makes, and whether to ask again."""

    locked: bool
    refused: bool = False
    permanent: bool = False
    # The thread this was asked to lock is not there any more, which is a different answer from
    # a refusal: nothing about the lock is wrong and asking again for the same thread can only
    # fail the same way. What it needs is the thread rebuilt.
    thread_missing: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """What the person who ran the command is told."""

    full_name: str
    number: int
    changed: bool
    locked: bool = False
    # Set when Discord refused the lock step, with the direction that was asked for. What to
    # tell somebody about a thread that would not lock and one that would not unlock is not the
    # same sentence: the second one means nobody can reply in it.
    lock_refused: bool = False
    wanted_locked: bool = False
    # Whether asking again could ever work. A missing permission cannot be waited out, and the
    # board poller is the one caller with nobody to tell, so it is the one that has to know the
    # difference between a refusal worth another poll and one that will refuse every poll.
    lock_refusal_is_permanent: bool = False
    # Set where the board had no column standing for what was just set. The change landed
    # everywhere that matters, so this is a caveat on a success rather than a failure - but
    # it is the one board refusal a person can see for themselves, because they will open
    # the board and find the card where it was. The others are invisible and identical for
    # every command, and saying them would be a warning attached to nothing they can do.
    board_has_no_column: bool = False
    # The label this moved, as the REPOSITORY spells it rather than as it was typed. Empty
    # for the seven commands that do not move an arbitrary one. The reply says it back, and
    # saying back what somebody typed would hide the one thing worth showing them: that
    # `/label BUG` wrote `bug`, because that is the label the repository actually has.
    label: str = ""


# How many of a repository's labels a refusal lists before it stops. A taxonomy can be long and
# the reply is one Discord message.
_ENOUGH_TO_SHOW = 15


class WhoIsMovingTheCard(Protocol):
    """The authorisation a particular member granted, for a card to be moved as them.

    One member, declared here because this is where it is consumed. Issue #170: before it, every
    card move went out under one shared token and GitHub recorded that account as having moved it,
    whoever had actually dragged anything. An empty string means they have granted none, which is a
    refusal rather than a fallback - moving a card as somebody else is the thing this replaced.
    """

    async def moving(self, *, guild_id: int, discord_user_id: int) -> str: ...


class MovesCards(Protocol):
    """Dragging an item's board card into the column standing for a status.

    Answers what became of it rather than raising for the ordinary ways there is nothing to
    do. The board is a mirror of the labels rather than the record, so none of them is worth
    failing a command that has already landed everywhere that counts - but one of them is
    worth mentioning, which is why the answer is not a bool.
    """

    @property
    def may_write(self) -> bool:
        """Whether this deployment writes to a board at all.

        Asked because the object itself is the only thing that knows. The flag is applied to the
        WRITER inside the board reader rather than by withholding the reader - issue #179, where
        withholding it also withheld the column-order rule - so a caller cannot tell from the
        wiring.

        One caller: the refusal that asks somebody to authorise. With writes off there is no write
        to authorise for, and asking anyway would refuse a command for a reason the person could do
        nothing about.
        """
        ...

    async def move_card(
        self,
        *,
        owner: str,
        project_number: int,
        card_id: int,
        state: Status | Priority,
        as_: str,
        column: str = "",
    ) -> CardMoved: ...

    async def order_for(
        self, *, owner: str, project_number: int, frm: Status, to: Status, column: str = ""
    ) -> BoardOrder | None: ...


@dataclass(frozen=True, slots=True)
class BoardCard:
    """Where an item's card is, for writing its column back.

    All three or none: a card id addresses nothing without the board it is on, and a board
    addresses nothing without an owner. Held together so that whether this can be written
    is one question rather than three that could be asked apart.
    """

    owner: str
    project_number: int
    card_id: int


class LabelsItems(Protocol):
    """Putting a label on an item, taking one off, and asking which ones exist.

    The third is only for the command that sets an arbitrary label: GitHub creates a name it has
    never seen rather than refusing, so a typo has to be caught here or it becomes a label on the
    repository for good.
    """

    async def list_labels(self, owner: str, name: str) -> Sequence[str]: ...

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None: ...


class ItemWorkflow:
    """Backs the status and priority commands.

    Every one of them is the same three steps with a different label: read the item as GitHub
    has it, put the labels right there, then bring the stored copy and the thread into line.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: LabelsItems,
        threads: ShutsAndKnowsServers,
        kinds: Mapping[ObjectType, ItemKind],
        repository_labels: RepositoryLabels,
        authorisations: WhoIsMovingTheCard,
        cards: MovesCards | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._one_item = ItemLock(sessionmaker)
        self._github = github
        self._threads = threads
        self._kinds = kinds
        self._labels = repository_labels
        # Turns the member who asked into the credential a card is moved with. Issue #170.
        self._authorisations = authorisations
        # None where this deployment has not turned board writes on, or has no project
        # token to make them with. Wiring rather than a flag read here, so a deployment
        # that has not opted in cannot reach the write at all.
        self._cards = cards

    async def set_status(
        self,
        *,
        thread_id: int,
        status: Status,
        column: str = "",
        acting: int | None = None,
    ) -> WorkflowOutcome:
        """Move an item to a status, and lock its thread once it is done.

        `acting` is the Discord member who asked, and None means nobody did - which is exactly
        one caller, the board poller. It calls this BECAUSE a card moved and would otherwise
        write the column it has just read straight back. It terminates either way - the next poll
        sees nothing further changed - so what it costs is a wasted call per moved card rather
        than a loop.

        It replaced a `tell_the_board` flag, and the type is the point. Since issue #170 a card is
        moved AS somebody, so "write to the board" and "whose authorisation to write with" are one
        question rather than two that can disagree. A board write with nobody to attribute it to is
        now unrepresentable rather than merely discouraged.

        `column` is the board column somebody picked, where they picked one. The status is still
        what drives the label, the lock and the block in Discord; the column decides only which
        of the board's own columns the card lands in, and it exists because a board may have two
        that mean one status. Empty where nothing named a column - the poller, and a repository
        mirroring no board - and then the status picks the column as it always did.
        """
        found = await locate(self._sessionmaker, thread_id)
        self._refuse_a_kind_it_cannot_move(found, instead="Move its card on the board instead.")
        snapshot = await self._fetch(found)
        self._refuse_conflicting_status(found, snapshot, status)
        if acting is not None:
            await self._refuse_a_move_the_board_forbids(found, status, column)
            await self._refuse_a_card_nobody_authorised(found, acting)

        change = labels.status_change(snapshot.label_names, status)
        if change.nothing_to_do and found.status is status:
            # Nothing to write, which is not the same as nothing to do. The lock is the last step
            # of a status change and the likeliest to have been refused on its own, so a repeat
            # is what gets it a second go.
            #
            # Both directions, and it used to be only one. Leaving DONE has to give the thread
            # back, and a refused unlock had nothing anywhere to try it again: the row already
            # says the new status, so the branch below never touches the lock either, and
            # `PullRequestPolicy.locked` returns None so no sync, webhook or `/pr` ever unlocks a
            # pull request's thread. One 503 shut a reopened pull request against the discussion
            # it had just been reopened for, permanently, while every later command answered that
            # it was already where it was being put.
            #
            # A closed issue cannot reach here asking to be unlocked: the guard above refuses any
            # status but DONE for one. That argument covered issues and only issues, and a pull
            # request has no such guard - so a MERGED one, whose thread the webhook shut on the
            # merge and whose row can still read IN_REVIEW because `PullRequestPolicy.status_for`
            # leaves the status alone, reached here asking for a status that is not DONE and had
            # its thread handed back. Nothing shuts it again: `locked` answers None on every sync
            # and `shut_for_state` wants a DONE the row does not hold.
            #
            # So the state of the ITEM decides too, not the status alone. A reopened pull request
            # is not closed, so the case this branch exists for - a refused unlock on something
            # reopened - still gets its second go.
            wants_lock = status is Status.DONE or snapshot.closed
            # Held across the lock this sets, because this is a Discord call the sync path never
            # makes and so never covered. An event for the same item can be in its own Discord
            # phase right now, and locking is the step where interleaving shows: it is last on
            # both sides and decided from what each read before it started. The rebuild inside
            # goes through the ordinary sync, which takes this same lock and is let straight
            # through, because a caller already holding it is not something to wait for.
            async with self._one_item.held(found.github_object_id):
                moved_to = await self._status_now(found.tracked_item_id)
                if moved_to is not status:
                    # Somebody moved the item while this waited, and their lock is the current
                    # one. Everything this branch acts on was read before the wait: the status
                    # off `locate`, the labels off a GitHub round trip after it. The branch
                    # below never had this problem, because it re-reads under the row's own lock
                    # and decides from what that gave back.
                    #
                    # Touching the lock anyway is not a stale write that rights itself. Locking
                    # shuts a thread against a state the row no longer holds, and for a pull
                    # request nothing lifts one: `PullRequestPolicy.locked` answers None on every
                    # sync and `shut_for_state` wants a DONE the row has moved off, so no
                    # webhook, sync or `/pr` reopens it. Unlocking is no safer, because the
                    # writer that moved it may have shut the thread on purpose a moment ago.
                    #
                    # Raised rather than answered, and raised as its own kind. Answering with
                    # an outcome was indistinguishable from the ordinary repeat that had nothing
                    # to do, so the person was told their item was where it is not and the board
                    # poller wrote the card off for good. A `WorkflowRefusedError` is no better
                    # from the poller's side: it reads that as the board asking for something
                    # the item cannot hold, which is final, and writes the column down for it.
                    # This is the opposite of final, so it is the one error here that wants the
                    # ordinary treatment: said to whoever ran it, and left for the next poll.
                    raise ItemMovedError(
                        f"{found.full_name}#{found.number} is "
                        f"{spoken(moved_to) if moved_to is not None else 'no longer tracked'} "
                        f"now, not {spoken(status)}: somebody moved it while this was "
                        f"running, so its thread was left alone. Run this again if you still "
                        f"want it {spoken(status)}."
                    )

                lock = await self._set_lock(thread_id, wants_lock, guild_id=found.guild_id)
                if lock.thread_missing:
                    thread_id, lock = await self._rebuild_and_lock(
                        found, snapshot, change, wants_lock, thread_id
                    )
                await self._write_the_lock_down(found.tracked_item_id, thread_id, lock)

            # The card too, for the reason the lock above is retried here: a swallowed
            # board write is permanent. Nothing retries it, the poller only ever rederives
            # a status FROM a column, and a card left behind because GitHub was having a
            # moment is noticed by nothing. Running the command again is what a person
            # does, and it answered 'already Done' and asked the board nothing.
            repeated = acting is not None and await self._move_the_card(
                found, status, acting=acting, column=column
            )
            return WorkflowOutcome(
                found.full_name,
                found.number,
                changed=False,
                locked=lock.locked,
                lock_refused=lock.refused,
                wanted_locked=wants_lock,
                lock_refusal_is_permanent=lock.permanent,
                board_has_no_column=repeated,
            )

        await self._apply(found, change)
        # Everything from the row write to the lock, held to this one writer. The labels
        # above are GitHub's and are left outside it: what two writers of one item can
        # spoil for each other is the thread, and holding a connection across a rate
        # limited GitHub call would be paying for the wrong thing.
        async with self._one_item.held(found.github_object_id):
            previous = await self._store_status(found.tracked_item_id, status)
            try:
                written = await self._rerender(found, snapshot, change, settles_the_lock=False)
            except BaseException:
                # BaseException because being cancelled counts as a failure here. The poller is
                # cancelled where it stands when the process is asked to stop, and a card being
                # moved at that moment would otherwise keep a row nothing in Discord shows, which
                # the next poll after a restart writes off. Shielded so the cancellation cannot
                # interrupt the putting back as well; under it the await returns at once and the
                # write lands a moment later.
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self._give_the_status_back(found.tracked_item_id, status, previous)
                    )
                raise

            # Touched only when DONE is on one side of the move or the other, so an ordinary
            # status change still costs no Discord call. Moving OUT of DONE has to give the
            # thread back: `PullRequestPolicy.locked` returns None on every sync, so the lock
            # `/status Done` takes is the only one a pull request ever gets and nothing else was
            # ever going to lift it. The commands to move it back are all allowed and all
            # reported success, and left the thread shut against the discussion they had just
            # reopened.
            wants_lock = status is Status.DONE
            touched = wants_lock or previous is Status.DONE
            lock = (
                await self._set_lock(written or thread_id, wants_lock, guild_id=found.guild_id)
                if touched
                else _Lock(locked=False)
            )
            if touched:
                await self._write_the_lock_down(found.tracked_item_id, written or thread_id, lock)

        no_column = acting is not None and await self._move_the_card(
            found, status, acting=acting, column=column
        )

        logger.info("%s#%s set to %s", found.full_name, found.number, status.value)
        return WorkflowOutcome(
            found.full_name,
            found.number,
            changed=True,
            locked=lock.locked,
            lock_refused=lock.refused,
            wanted_locked=wants_lock,
            lock_refusal_is_permanent=lock.permanent,
            board_has_no_column=no_column,
        )

    async def set_priority(
        self, *, thread_id: int, priority: Priority, acting: int | None = None
    ) -> WorkflowOutcome:
        """Move an item to a priority. Nothing is locked and no status moves with it.

        The stored priority has to agree as well as the label, which is the same rule the status
        half above follows and for the same reason: a run that puts the label on GitHub and then
        cannot reach Discord leaves the thread saying the old one. Asking GitHub alone, the
        repeat that is meant to repair that answers "already HIGH priority" and writes nothing,
        so the block stays wrong until some unrelated event for the item arrives, which for a
        merged pull request is never. Reproduced: label HIGH on GitHub, HIGH in the row, UNSET
        in the thread, and the command that exists to fix it reporting nothing to do.
        """
        found = await locate(self._sessionmaker, thread_id)
        self._refuse_a_kind_it_cannot_move(found, instead="Move its card on the board instead.")
        snapshot = await self._fetch(found)

        change = labels.priority_change(snapshot.label_names, priority)
        if change.nothing_to_do and found.priority is priority:
            # The board is asked on a repeat too, the same as the status half. Both
            # commands answer the same question and a repeat means the same thing in both:
            # try the half that has nothing else to retry it.
            repeated = acting is not None and await self._move_the_card(
                found, priority, acting=acting
            )
            return WorkflowOutcome(
                found.full_name, found.number, changed=False, board_has_no_column=repeated
            )

        await self._apply(found, change)
        await self._rerender(found, snapshot, change)

        # `acting` means the same thing here as on the status half: whoever asked, and None for
        # nobody. It is optional for the same reason too, which is worth being plain about -
        # the alternative was a required argument on a path most callers reach without caring
        # about the board at all, and a default of None costs nothing because it is the safe
        # answer: no member, no board write. The two production callers both pass one.
        #
        # There is no poller on this path, unlike the status half:
        # the poller, which calls `set_status` BECAUSE a card moved - and nothing polls a
        # priority: the poll reads a card's column and no other field. A parameter no
        # caller ever passes False would be an arm only a test could take.
        no_column = acting is not None and await self._move_the_card(found, priority, acting=acting)

        logger.info("%s#%s set to %s priority", found.full_name, found.number, priority.value)
        return WorkflowOutcome(
            found.full_name, found.number, changed=True, board_has_no_column=no_column
        )

    async def set_label(self, *, thread_id: int, name: str, adding: bool) -> WorkflowOutcome:
        """Put an ordinary label on an item, or take one off.

        Modelled on `set_priority` rather than on `set_status`: no stored column moves, so there
        is no row to write, nothing to put back when Discord fails, and no thread to lock.

        The two refusals are the whole of this command. Everything else here already existed.
        """
        found = await locate(self._sessionmaker, thread_id)
        self._refuse_a_kind_it_cannot_move(
            found,
            instead="A draft card has no labels on GitHub. Convert it to an issue there and "
            "this bot will track the issue from its own page.",
        )
        self._refuse_a_name_this_bot_owns(name)

        # Before the labels are listed, not after. Listing addresses GitHub by the stored
        # `owner/name`, and if that name has been taken by somebody else the refusal would read a
        # stranger's label taxonomy back to whoever ran the command.
        snapshot = await self._fetch(found)
        spelled = await self._spelling_the_repository_uses(found, name)

        change = labels.label_change(snapshot.label_names, spelled, adding=adding)
        if change.nothing_to_do:
            return WorkflowOutcome(found.full_name, found.number, changed=False, label=spelled)

        await self._apply(found, change)
        await self._rerender(found, snapshot, change)

        logger.info(
            "%s#%s %s %r",
            found.full_name,
            found.number,
            "was labelled" if adding else "lost the label",
            spelled,
        )
        return WorkflowOutcome(found.full_name, found.number, changed=True, label=spelled)

    async def labels_for_thread(self, thread_id: int) -> tuple[str, ...]:
        """Every label the repository behind this thread has, for the picker.

        Asked on every keystroke, which is why the list is cached a repository at a time. A thread
        that is not a tracked item answers with nothing rather than raising: an autocomplete has
        nowhere to put a refusal, and an empty picker in a channel that is not an item's thread is
        the right amount of nothing to say.

        A kind this service cannot write to is the same nothing, and used not to be. A project
        ticket's thread is a tracked item, so it got past the guard above and was offered every
        label the REGISTERED REPOSITORY has - a full, working-looking picker where `set_label`
        then refused all of them. Answered off the same predicate the refusal uses, so the two
        cannot drift apart again.
        """
        try:
            found = await locate(self._sessionmaker, thread_id)
        except NotAnItemThreadError:
            return ()
        if not self._can_be_moved(found):
            return ()
        return await self._labels.names(found.owner, found.name)

    def _refuse_a_name_this_bot_owns(self, name: str) -> None:
        """Refuse a label that already means something here, and say how to set it properly.

        A status set this way would make the block contradict itself. Nothing on the webhook path
        reads a status back onto the stored column, so the item would go on showing one status
        while carrying the label of another, and the thread would post a line saying the status
        had been set when it had not moved at all.

        Priority fails the other way and is worse for it. `parse_priority` DOES feed the stored
        column on every sync, so `critical` written here changes an item's priority from a command
        that never mentioned priority.
        """
        reserved = labels.reserved_as(name)
        if reserved is None:
            return
        # The word rather than a command name. There used to be a table here mapping each state
        # to the command that owned it, written out because MEDIUM's was `set_med_priority` and
        # not `set_medium_priority` - a derived name would have been wrong for exactly one of the
        # eight and right everywhere it was tested. With the state a choice rather than a command
        # name, nothing about it is underivable and the table has nothing left to say.
        if isinstance(reserved, Status):
            raise WorkflowRefusedError(
                f"{code_span(name)} is a workflow status here. Run /status and pick "
                f"{spoken(reserved)} instead."
            )
        raise WorkflowRefusedError(
            f"{code_span(name)} already means {spoken(reserved)} priority here. Run /priority "
            f"and pick {spoken(reserved)} instead."
        )

    async def _spelling_the_repository_uses(self, found: FoundItem, name: str) -> str:
        """The repository's own spelling of this label, refusing one it does not have.

        Refused rather than written, because GitHub creates a label it has never seen instead of
        refusing. That is what lets the workflow commands work on a repository nobody prepared,
        and it is exactly wrong for a name somebody typed: one slip adds a label to the repository
        for good, and nothing here can list or delete one afterwards.
        """
        spelled = await self._labels.spelled(found.owner, found.name, name)
        if spelled is not None:
            return spelled

        known = sorted(await self._labels.names(found.owner, found.name))
        if not known:
            raise WorkflowRefusedError(
                f"{found.full_name} has no labels at all yet, so there is none to set. Make one "
                "on GitHub first."
            )
        shown = ", ".join(known[:_ENOUGH_TO_SHOW])
        rest = len(known) - _ENOUGH_TO_SHOW
        raise WorkflowRefusedError(
            f"{found.full_name} has no label called {code_span(name)}. It has: {shown}"
            + (f", and {rest} more." if rest > 1 else ", and one more." if rest == 1 else ".")
        )

    async def _move_the_card(
        self, found: FoundItem, state: Status | Priority, *, acting: int, column: str = ""
    ) -> bool:
        """Drag this item's board card to match, where there is one and it may be.

        Answers whether the board had nowhere to put it, which is the one refusal worth
        passing back. Everything that matters has already landed by this point: the labels
        are on GitHub, the row is written and the thread is redrawn. So a failure here is
        logged and swallowed rather than raised - told to whoever ran the command it would
        read as the change having failed, which it did not.

        A 422 is the one worth reading in the log. `_raise_for_status` carries GitHub's own
        words, and this write's body shape was taken from published documentation rather
        than from a live board, so that message is the first real evidence either way.
        """
        if self._cards is None or found.card is None:
            return False

        try:
            moved = await self._cards.move_card(
                owner=found.card.owner,
                project_number=found.card.project_number,
                card_id=found.card.card_id,
                state=state,
                # Whoever asked. `_refuse_a_card_nobody_authorised` has already established that
                # they granted one, so this is a lookup rather than a question.
                as_=await self._authorisations.moving(
                    guild_id=found.guild_id, discord_user_id=acting
                ),
                column=column,
            )
        except ShannonError as refused:
            logger.warning(
                "%s#%s is %s here, but its card on board %s was left where it was: %s",
                found.full_name,
                found.number,
                spoken(state),
                found.card.project_number,
                refused.message,
            )
            return False

        if moved.outcome is CardMove.MOVED and isinstance(state, Status):
            # The column the BOARD calls it, not the state that was asked for. The
            # poller compares the column it last saw by text, so leaving this stale
            # makes the next poll read this bot's own write as somebody dragging the
            # card - and worse, a real drag BACK to the old column inside one poll
            # interval reads as never having moved and is dropped for good.
            #
            # Status alone. The stored column is the STATUS column: a priority lands
            # in a different field entirely, and writing its option name here would
            # tell the poller the card had moved to a column called HIGH.
            async with self._sessionmaker() as session, session.begin():
                await TrackedItemStore(session).remember_column(found.tracked_item_id, moved.column)
        return moved.outcome is CardMove.NO_COLUMN

    def _can_be_moved(self, found: FoundItem) -> bool:
        """Whether this service has any way to write to the item behind a thread.

        One predicate rather than two, because the refusal below and the picker in
        `labels_for_thread` were answering it separately and disagreeing: the picker offered a
        draft card the whole repository's labels and the refusal then turned every one of them
        down.
        """
        return found.object_type in self._kinds

    async def _refuse_a_card_nobody_authorised(self, found: FoundItem, acting: int) -> None:
        """Refuse before anything is written, where this member has authorised no board access.

        Issue #170. A card is moved AS the person who asked, so somebody who has granted nothing
        cannot move one - and this is the one board refusal they can fix themselves, in one
        command, which is what makes refusing better than carrying on quietly.

        Deliberately NOT the shape the other board failures take. Those are logged and swallowed
        after the labels, the row and the thread have landed, on the argument that a warning
        attached to a fix the caller cannot make is noise. That argument inverts here: the caller
        CAN make the fix, so the reply is worth having - and it comes first, before anything has
        happened, so there is no half-done change to explain.

        Only where there is a write to make. Two ways there is not, and both would otherwise
        refuse a command for a reason the person could do nothing about:

        - the item has no card, because nobody added it to the board;
        - this deployment has board writes turned off, so no credential of anybody's would be
          used. Asking somebody to authorise access for a write that is not going to happen is
          worse than saying nothing. That is `may_write` rather than a None check, because the
          flag is applied to the WRITER inside the board reader and not by withholding it.
        """
        if self._cards is None or not self._cards.may_write or found.card is None:
            return
        if not await self._authorisations.moving(guild_id=found.guild_id, discord_user_id=acting):
            raise WorkflowRefusedError(
                "This server mirrors a project board and you have not authorised this bot to "
                "move cards as you, so nothing was changed. Run /authorise_board and sign in to "
                "GitHub: a card is moved as YOU, so the board's history names whoever moved it "
                "rather than one shared account."
            )

    def _refuse_a_kind_it_cannot_move(self, found: FoundItem, *, instead: str) -> None:
        """Refuse a thread whose item this service has no way to write to.

        A project ticket is a draft card on a board. It has no repository page and no labels, so
        there is nothing here to set: its status is the column it sits in, and the board is where
        that gets changed.

        The KeyError this stands in front of is the smaller half of why it exists. A draft card
        has no number, so `_snapshot` carries the BOARD's number in that slot and `FoundItem.of`
        reads it straight back. Drop the guard and nothing raises: the write goes to
        `/repos/{owner}/{name}/issues/{project_number}/labels` and lands on whatever issue or
        pull request happens to hold that number, under a reply naming an item nobody asked
        about. Widening `_kinds` with a TICKET entry does not fix that, it hides it - there is no
        `(owner, name, number)` that addresses a draft.

        `instead` is the one sentence that differs by caller. Required rather than defaulted:
        three callers, three sentences, and a default would be an arm nothing reaches. Telling
        somebody to move the card is right for a status and wrong for a label, which is a column
        rather than a label and was the advice this gave for both.
        """
        if self._can_be_moved(found):
            return
        raise WorkflowRefusedError(
            f"That thread is a project {found.object_type.value.lower()}, which has no "
            f"GitHub labels to set. {instead}"
        )

    def _refuse_conflicting_status(
        self, found: FoundItem, snapshot: TrackedSnapshot, status: Status
    ) -> None:
        """Refuse, rather than write a status that something else is going to overwrite.

        An issue's status is not this service's alone to decide. The requirements make closing
        an issue mean done, and the sync path enforces that on every delivery, so both
        directions of disagreement have to be refused here: marking an open issue done, and
        marking a closed one anything else. Writing either would put the label on GitHub,
        report the change as made, and then have the very next render take it back.
        """
        if found.object_type is ObjectType.ISSUE:
            if snapshot.closed and status is not Status.DONE:
                raise WorkflowRefusedError(
                    f"That issue is closed on GitHub, which is what makes it "
                    f"{spoken(Status.DONE)}. Reopen it there to give it another status."
                )
            if not snapshot.closed and status is Status.DONE:
                raise WorkflowRefusedError(
                    f"Close the issue on GitHub to mark it {spoken(Status.DONE).lower()}; "
                    "that locks the thread too."
                )
            return

    async def _refuse_a_move_the_board_forbids(
        self, found: FoundItem, status: Status, column: str = ""
    ) -> None:
        """Refuse a jump the board's own column order does not allow.

        This replaced a rule written out in Python - a pull request had to be `Ready for merge`
        before `Done` - with the same requirement read off whatever columns a board has. The
        old constant could only ever be right for a board that happened to use that word, and
        GitHub's own default template does not.

        Only when somebody is acting, which is exactly not the poller. The poller calls
        BECAUSE a card has already moved: somebody dragging one is the fact being mirrored
        rather than a request to be judged, and a poll has nowhere to put a refusal anyway.
        Refusing there would leave the board and the row disagreeing for ever, with the card
        where the person put it and the status where it was.

        Silent wherever there is nothing to ask: no writer, no card, no board, or a board whose
        columns will not read. The rule comes from a list this bot did not write, so it fails
        open the way every other board read on this path does - a GitHub outage must not take
        `/status` down for everybody.
        """
        if self._cards is None or found.card is None:
            return

        try:
            order = await self._cards.order_for(
                owner=found.card.owner,
                project_number=found.card.project_number,
                frm=found.status,
                to=status,
                column=column,
            )
        except ShannonError as unreadable:
            logger.warning(
                "could not read board %s to check the move for %s#%s: %s",
                found.card.project_number,
                found.full_name,
                found.number,
                unreadable.message,
            )
            return
        if order is None:
            return

        # The column the card is actually in wins over the one this item's status maps to.
        # They differ on a board with two columns reading as one status - GitHub's template
        # ships `In progress` and `In review`, both IN_REVIEW - and the card's own column is
        # the one the next move is measured from.
        skipped = must_pass_through(
            order.columns, frm=found.column or order.leaving, to=order.arriving
        )
        if not skipped:
            return
        raise WorkflowRefusedError(
            f"Its board goes {' -> '.join(order.columns)}, so moving that card to "
            f"{order.arriving} would skip {', '.join(skipped)}. Move it to {skipped[0]} first, "
            "or drag the card on the board yourself."
        )

    async def _fetch(self, found: FoundItem) -> TrackedSnapshot:
        """Read the item from GitHub, and refuse anything that is not the repository we mean.

        Everything below this addresses GitHub by the stored `owner/name`, and a name is not an
        identity. GitHub frees one the moment a repository is renamed, transferred or deleted,
        and the stored one goes stale by design: nothing corrects it until an item webhook
        arrives, and for a repository that has been renamed away no webhook ever will.

        So the path this asks about can be somebody else's repository by the time it is asked.
        Unchecked, the labels were written onto their item, the re-render resolved the fetched
        snapshot by its own id and opened a thread in whichever server had registered it, and
        `/status Done` locked that thread rather than the one the command was run in. The reviewer
        was told it worked and their own thread never changed.

        The check is free. The snapshot already carries the id, and comparing it costs no call.
        """
        snapshot = await self._kinds[found.object_type].fetch(found.owner, found.name, found.number)
        if snapshot.repository.github_repo_id != found.github_repo_id:
            raise WorkflowRefusedError(
                f"{found.full_name} is not the repository this server registered any more. "
                "It has been renamed or replaced on GitHub, and somebody else holds that name "
                "now. Register the repository again under its current name."
            )
        return snapshot

    async def _apply(self, found: FoundItem, change: labels.LabelChange) -> None:
        """Put the labels right on GitHub.

        Removals first. The two states this can be interrupted in are an item with no status
        label and an item with two, and the first is the one a reader can make sense of.
        """
        for name in change.remove:
            await self._github.remove_label(found.owner, found.name, found.number, name)
        if change.add:
            await self._github.add_label(found.owner, found.name, found.number, change.add)

    async def _rerender(
        self,
        found: FoundItem,
        snapshot: TrackedSnapshot,
        change: labels.LabelChange,
        *,
        settles_the_lock: bool = True,
    ) -> int | None:
        """Bring the thread in line, through the same path a webhook takes.

        The snapshot is carried forward with its labels corrected rather than fetched again.
        Re-syncing the one that was read before the write would take the priority straight back
        off the labels it no longer has, which is the change undoing itself.

        Answers with the thread that was actually written to. It is usually the one the command
        was run in, and is not when somebody deleted that thread in between: the sync opens a
        replacement, and locking the id the command arrived on would lock nothing.
        """
        result = await self._kinds[found.object_type].sync.sync(
            _relabelled(snapshot, change), settles_the_lock=settles_the_lock
        )
        return result.thread_id

    async def _status_now(self, tracked_item_id: int) -> Status | None:
        """What the row says this moment, or None for a row that is no longer there.

        Asked inside the hold and nowhere else. A repeat exists to give a lock that was refused
        another go, and what makes it a repeat is the row already saying what is being asked for.
        That was read before the wait for the hold, and the whole point of the wait is that
        somebody else is writing.

        Answers with the status rather than with yes or no, because whoever is told about this
        wants to know what it moved to, and this is the only place that knows.
        """
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
        return item.status if item is not None else None

    async def _set_lock(self, thread_id: int, locked: bool, *, guild_id: int) -> _Lock:
        """Close a finished item's thread to further replies, or open it again.

        Last, after the metadata is written. A locked thread still takes this bot's edits, so
        the order is not what makes it work; it is that the lock is the step most likely to be
        refused, and everything before it is worth keeping when it is.

        Answers whether the lock is where it was asked to be, and separately whether Discord
        refused to put it there. Raising instead is what this used to do, and it told the person
        who ran the command that the whole thing had failed, when everything before this had
        landed: the labels are on GitHub, the status is in the row, the thread says so. The two
        readings are a long way apart for somebody deciding whether to run it again, and a
        refusal here is usually one permission rather than anything to wait out.

        Only the gateway errors, and only around this call. Anything else still raises, and
        anything that fails before this still fails the command outright, because then nothing
        did happen.

        Whether a refusal is permanent is not the exception type alone. A bot that has been
        removed from the server is answered exactly as one that was never given the permission,
        and the two want opposite things: nobody grants a permission by waiting, and nobody
        fixes an absence any other way. It matters most where nobody is watching. The board
        poller writes a move off as carried through on a permanent refusal, deliberately, so
        that one missing permission does not put every card ever dragged to Done into a set
        retried once a minute for ever. Filed that way while the bot is out for five minutes,
        the card is recorded as moved with its thread left open and no poll looks at it again.
        """
        try:
            await self._threads.set_shut(thread_id=thread_id, shut=locked)
        except ThreadNotFoundError as error:
            logger.info("thread %s is gone, so there was nothing to lock: %s", thread_id, error)
            return _Lock(locked=False, refused=True, thread_missing=True)
        except DiscordGatewayError as error:
            logger.warning("could not set the lock on thread %s: %s", thread_id, error.message)
            return _Lock(
                locked=False,
                refused=True,
                permanent=isinstance(error, PermanentError) and self._threads.is_in(guild_id),
            )
        return _Lock(locked=locked)

    async def _store_status(self, tracked_item_id: int, status: Status) -> Status:
        """Written before the re-render, because the render reads it back off the row.

        Answers with the status it replaced, read under the row's own lock. Whether the thread
        gets locked or given back is decided from that and not from the read at the top of the
        command, because three GitHub round trips sit in between and two commands overlapping
        across them both decided from a row neither of them still had.

        What that cost: a pull request at READY_FOR_MERGE, a `/status Done` and a
        `/status In review` from two reviewers, or from a reviewer and the board poller. The one
        that was not finishing the item read a status that was not DONE yet, so it never asked
        for the thread back, while `/status Done` locked it last. The item was left reading
        IN_REVIEW with its thread shut, both users were told their command had worked, and
        nothing lifted it: `PullRequestPolicy.locked` returns None, so no webhook or sync ever
        unlocks a pull request, and `/status Done` is refused for being exactly what the race
        made it.
        """
        async with self._sessionmaker() as session, session.begin():
            item = await TrackedItemStore(session).get_by_id(tracked_item_id, lock=True)
            if item is None:
                raise ItemNotReadyError("That item is no longer tracked here.")
            previous = item.status
            item.status = status
            return previous

    async def _write_the_lock_down(self, tracked_item_id: int, thread_id: int, lock: _Lock) -> None:
        """Tell the row what this command just made the thread, so the sync path stops asking.

        These commands own the lock on a pull request: nothing else takes one, and the sync is
        told to leave it alone on this path so a refusal reaches the person who ran the command
        rather than failing everything before it. The row is how the two halves agree. Without
        this it never hears, so it goes on reading a finished pull request as one whose thread
        has not been shut: every later delivery asks Discord to shut a thread already shut, and
        the staleness guard, which lets a delivery through while a lock is owed, lets every
        superseded delivery for that item straight past a guard that exists to stop an old
        payload overwriting newer state.

        Nothing is written for a refusal. The thread is not where it was asked to be, and the row
        saying otherwise is the one mistake that cannot be recovered from here.
        """
        if lock.refused:
            return
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).note_the_lock(
                tracked_item_id, thread_id=thread_id, locked=lock.locked
            )

    async def _rebuild_and_lock(
        self,
        found: FoundItem,
        snapshot: TrackedSnapshot,
        change: labels.LabelChange,
        wants_lock: bool,
        thread_id: int,
    ) -> tuple[int, _Lock]:
        """Open a replacement for a thread that has gone, and lock that one instead.

        Only reached from the branch with nothing to write, which is the one branch that skips
        the render. Everywhere else the render runs first and rebuilds a missing thread on its
        own, because the write path turns Discord saying a thread is gone into a replacement.

        Here the lock is the only Discord call made, and it is the one step that needs the thread
        to already exist, so a thread deleted while this bot was not connected to hear about it
        could be noticed here and repaired nowhere. For a card on a board that never ended: the
        poller reads a refused lock as a bad moment worth another go, the column is what ends a
        retry and is deliberately not written, and the one thing that would have rebuilt the
        thread sits on the other side of the branch. A GitHub read, a thread fetch and two
        warnings for that card, once a minute, with nothing able to clear it.
        """
        rebuilt = await self._rerender(found, snapshot, change, settles_the_lock=False)
        # The one the command arrived on, where the render answered with nothing. It used to
        # reach for a field `FoundItem` does not have, which nothing noticed because nothing here
        # is type checked and every route to it is closed by an invariant somewhere else: a
        # repository row is never deleted, a channel mapping is never deleted, and a ticket is
        # refused before this. All true, and none of them stated anywhere near this line.
        on = rebuilt or thread_id
        return on, await self._set_lock(on, wants_lock, guild_id=found.guild_id)

    async def _give_the_status_back(
        self, tracked_item_id: int, written: Status, previous: Status
    ) -> None:
        """Put the row back when the step after it failed, so the move can be asked for again.

        The status has to be written before the thread is rewritten, because the render reads it
        off the row. So a render that fails leaves the row saying a move happened that nobody can
        see, and for a card on a board that is the end of it. The poller's entire retry is the
        column not being recorded, and both of its first-look guards read a card whose status
        already agrees and whose column was never written down as one it has never seen. The move
        it could not finish is written off on the very next poll, and no poll looks at that card
        again, because nothing else rederives a status from a board. A person running the command
        at least sees it fail and can run it again; nobody is standing over the poller.

        Only where the row still says what this wrote, which is enough for one process and not
        for two. Nothing in the deployment stops a second replica, and every replica with a
        project number set polls the same board on the same interval, so the two ask for the same
        status for the same card. The other one, polling while this one is in Discord, finds the
        row already saying DONE and records the column, which is this one's whole retry marker;
        then this one cannot tell that from its own write and puts the row back, undoing a move
        the other had finished. Comparing a stamp instead does not help: the render's own sync
        writes the row before the Discord call it dies on, so the stamp has moved in the ordinary
        single-process case too, and nothing on the row separates somebody else having acted from
        the step being compensated for.

        That is the shape of a compensating write rather than a fault in this guard, and the
        per-item lock does not close it, though it was written down as the thing that would. The
        lock is taken here, around the write and the render and the compensation together, so no
        other writer can be in the middle of this one. What the other poller acts on is not read
        here: it decides from a batch of rows read before its move loop starts, a service and a
        step earlier, so it has already read DONE off the row by the time it asks for anything
        this could hold it out of. Closing it means the decision being made against the row it is
        acted on, not against a snapshot. Until then the poller belongs in one replica, which is
        said where the setting that enables it is defined.

        The label on GitHub is left where it was put. Setting it is idempotent, the next attempt
        sets it again, and somebody reading GitHub in between sees where the card was dragged
        rather than a value that flickers back on its own.

        Its own failure is said out loud rather than raised, because the failure worth reporting
        is the one that brought us here.
        """
        try:
            async with self._sessionmaker() as session, session.begin():
                item = await TrackedItemStore(session).get_by_id(tracked_item_id, lock=True)
                if item is not None and item.status is written:
                    item.status = previous
        except Exception:
            logger.warning(
                "could not put tracked item %s back to %s after the move failed; it now reads "
                "%s with nothing in Discord to show it",
                tracked_item_id,
                previous.value,
                written.value,
                exc_info=True,
            )


def _relabelled(snapshot: TrackedSnapshot, change: labels.LabelChange) -> TrackedSnapshot:
    """The snapshot as it will be once the change lands, without asking GitHub again.

    Only appends a label the item is not already carrying. An item can hold two labels the same
    reader answers for, such as `HIGH` beside `urgent` or two statuses at once, and the change then
    strips one and adds the canonical name, which the item already had. Appending it regardless
    rendered the tag twice in the thread, and which of the two happened depended on the order
    GitHub returned the labels in, so the same item state produced different blocks.
    """
    gone = {name.casefold() for name in change.remove}
    kept = [label for label in snapshot.labels if label.name.casefold() not in gone]
    if change.add and change.add.casefold() not in {label.name.casefold() for label in kept}:
        kept.append(Label(name=change.add))
    return snapshot.relabelled(tuple(kept))


async def locate(sessionmaker: async_sessionmaker[AsyncSession], thread_id: int) -> FoundItem:
    """Which item a thread is, as plain values out of the session.

    A function rather than a method, because it is the one question every command run INSIDE a
    thread has to ask first and it belongs to no single service. The workflow commands ask it to
    decide what to relabel; `/regenerate` asks it to decide what to redraw.

    The repository is fetched rather than read off `item.repository`: that is a lazy
    relationship, and an async session cannot load one on attribute access.
    """
    async with sessionmaker() as session:
        item = await TrackedItemStore(session).get_by_thread(thread_id)
        repository = (
            await RepositoryStore(session).get_by_id(item.repository_id)
            if item is not None
            else None
        )
        if item is None or repository is None:
            raise NotAnItemThreadError(
                "Run this inside the thread of a pull request or issue this bot is tracking."
            )
        return FoundItem.of(item, repository)


@dataclass(frozen=True, slots=True)
class FoundItem:
    """The item a thread belongs to, as plain values out of its session."""

    tracked_item_id: int
    object_type: ObjectType
    full_name: str
    # What the repository actually is. The name is only what GitHub called it when something
    # last told us, and GitHub frees a name the moment a repository is renamed or deleted.
    github_repo_id: int
    number: int
    # What the item lock is keyed on, and the only id that is the item's own:
    # the number is unique per repository and this is unique across GitHub.
    github_object_id: int
    # The server the thread is in, for telling a refusal from a bot that has been removed
    # apart from a permission it was never given. Discord answers both the same way.
    guild_id: int
    status: Status
    priority: Priority
    # Where this item's board card is, or None where no board is linked to the
    # repository or no poll has yet paired the two.
    card: BoardCard | None = None
    # The column the card was last seen in, in the BOARD's own spelling, or None where no
    # poll or write has recorded one. Kept beside `status` rather than derived from it
    # because a board may have two columns reading as one status, and which of them a card
    # actually sits in is what decides whether the next move skips anything.
    column: str | None = None

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]

    @classmethod
    def of(cls, item: TrackedItem, repository: Repository) -> FoundItem:
        return cls(
            tracked_item_id=item.id,
            object_type=item.github_object_type,
            full_name=repository.repo_name,
            github_repo_id=repository.github_repo_id,
            number=item.github_object_number,
            github_object_id=item.github_object_id,
            guild_id=repository.discord_guild_id,
            status=item.status,
            priority=item.priority,
            column=item.project_column,
            card=(
                BoardCard(
                    # Never empty, and the resolver says why: an empty owner sends the write
                    # out with no credential at all.
                    owner=board_owner(
                        project_owner=repository.project_owner,
                        repo_name=repository.repo_name,
                    ),
                    project_number=number,
                    card_id=card_id,
                )
                if (number := repository.project_number) is not None
                and (card_id := item.project_item_id) is not None
                else None
            ),
        )


def build_item_workflow(
    sessionmaker: async_sessionmaker[AsyncSession],
    github: GitHubClient,
    threads: ShutsAndKnowsServers,
    *,
    pr_sync: SyncsItems,
    issue_sync: SyncsItems,
    authorisations: WhoIsMovingTheCard,
    cards: MovesCards | None = None,
) -> ItemWorkflow:
    """Assemble the workflow service with a fetcher and a renderer per object type."""
    return ItemWorkflow(
        sessionmaker,
        github,
        threads,
        {
            ObjectType.PR: ItemKind(
                fetch=lambda owner, name, number: github.get_pull_request(owner, name, number),
                sync=pr_sync,
            ),
            ObjectType.ISSUE: ItemKind(
                fetch=lambda owner, name, number: github.get_issue(owner, name, number),
                sync=issue_sync,
            ),
        },
        RepositoryLabels(github),
        authorisations,
        cards,
    )
