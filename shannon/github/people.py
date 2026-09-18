"""Whether GitHub would take somebody being put on an item, worked out before it is asked.

Nothing here talks to GitHub. It decides, and the client does as it is told, which is the split
`labels.py` next door already makes for the same reason: the decision is where the thinking is and
it should be testable without a socket.

Two of these refusals GitHub would make anyway, with a 422 and a round trip. Making them here is
not about saving the call, it is about the sentence. GitHub's own words for the author case name
the endpoint and the collaborator rule, which is true and is not what somebody in a Discord thread
needed to be told.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shannon.domain.models import Actor, PullRequestSnapshot, TrackedSnapshot


@dataclass(frozen=True, slots=True)
class PeopleChange:
    """Whether to ask GitHub for this, and what to say instead if not.

    Both halves on one object because the caller needs exactly one of them, and which one is the
    entire question. `refusal` is a sentence for whoever ran the command rather than a log line.
    """

    login: str
    refusal: str | None = None

    @property
    def wanted(self) -> bool:
        return self.refusal is None


def reviewer_change(login: str, snapshot: PullRequestSnapshot, *, adding: bool) -> PeopleChange:
    """Whether to ask for, or withdraw, a review from this account.

    The author check has no counterpart below, and that asymmetry is GitHub's rather than a
    decision made here: a pull request may be ASSIGNED to whoever opened it, and cannot have a
    review REQUESTED from them. Asking anyway is a 422 reading as though the person were not a
    collaborator at all, which for the author of the thing is a confusing way to be refused.
    """
    if adding and _is(login, snapshot.author):
        return PeopleChange(
            login,
            f"{login} opened this pull request, so GitHub will not ask them to review it.",
        )
    return _held(login, snapshot.reviewers, adding=adding, asked="asked to review this")


def assignee_change(login: str, snapshot: TrackedSnapshot, *, adding: bool) -> PeopleChange:
    """Whether to put this account on the item, or take it off.

    Takes the protocol rather than the concrete issue, because it reads only `assignees` and both
    kinds of item carry that. No author check, for the reason the reviewer one gives. Nor any test
    of whether GitHub would accept the account at all: that is `can_be_assigned`, and it needs the
    network, because a snapshot says who IS on an item and never who COULD be.
    """
    return _held(login, snapshot.assignees, adding=adding, asked="assigned to this")


def _held(login: str, people: Iterable[Actor], *, adding: bool, asked: str) -> PeopleChange:
    """Refuse a change that has already happened, in either direction.

    Refused rather than passed through as a harmless repeat. Adding twice is a 422 and removing
    somebody who was never there is a 404 the client swallows, so without this the first reports
    an error nobody caused and the second reports success for nothing at all.
    """
    on_it = any(_is(login, person) for person in people)
    if adding and on_it:
        return PeopleChange(login, f"{login} has already been {asked}.")
    if not adding and not on_it:
        return PeopleChange(login, f"{login} has not been {asked}.")
    return PeopleChange(login)


def _is(login: str, person: Actor | None) -> bool:
    """Whether these name the same account, compared the way GitHub compares logins.

    Case-insensitively, because GitHub treats them that way and the stores lowercase for the same
    reason. A login read off a snapshot and one read out of `user_links` have been through
    different hands and need not agree on case.
    """
    return person is not None and person.login.casefold() == login.casefold()
