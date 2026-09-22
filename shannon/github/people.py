"""Whether GitHub would take somebody being put on an item, worked out before it is asked.

Nothing here talks to GitHub. Two of these refusals GitHub would make anyway with a 422, and are
made here for the sentence: GitHub's own wording names the endpoint and the collaborator rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from shannon.domain.models import Actor, PullRequestSnapshot, TrackedSnapshot


@dataclass(frozen=True, slots=True)
class PeopleChange:
    """Whether to ask GitHub for this, and what to say instead if not.

    `refusal` is a sentence for whoever ran the command rather than a log line.
    """

    login: str
    refusal: str | None = None

    @property
    def wanted(self) -> bool:
        return self.refusal is None


def reviewer_change(login: str, snapshot: PullRequestSnapshot, *, adding: bool) -> PeopleChange:
    """Whether to ask for, or withdraw, a review from this account.

    GitHub's asymmetry: a pull request may be assigned to its author but cannot have a review
    requested from them, and asking anyway is a 422 that reads as not a collaborator at all.
    """
    if adding and _is(login, snapshot.author):
        return PeopleChange(
            login,
            f"{login} opened this pull request, so GitHub will not ask them to review it.",
        )
    return _held(login, snapshot.reviewers, adding=adding, asked="asked to review this")


def assignee_change(login: str, snapshot: TrackedSnapshot, *, adding: bool) -> PeopleChange:
    """Whether to put this account on the item, or take it off.

    No test of whether GitHub would accept the account at all: that is `can_be_assigned`, which
    needs the network, since a snapshot says who is on an item and never who could be.
    """
    return _held(login, snapshot.assignees, adding=adding, asked="assigned to this")


# GitHub's whole ladder, as it answers rather than as its settings page reads: it folds
# `maintain` onto `write` and `triage` onto `read` before replying, so these two names and
# everything else between them cover every answer there is.
NO_ACCESS: Final = "none"
READ_ONLY: Final = "read"


def assignment_refusal(login: str, full_name: str, permission: str) -> str:
    """Why GitHub would not take this account on an item, once it has already said it would not.

    Issue #133. What stood here was one sentence for every refusal, guessing that the person had
    no access to the repository. It was wrong for the case that brought this about: a
    collaborator with write access whose login had moved since somebody linked it, so the name
    GitHub was asked about belonged to nobody. They were told they were not in a repository they
    had every access to.

    So the permission is read rather than assumed, and each answer gets the sentence that is true
    of it. The last one is deliberately not a guess: two GitHub answers disagreeing is not
    something this can explain, and saying so is better than inventing a reason.
    """
    if permission == NO_ACCESS:
        return (
            f"GitHub does not have {login} as a collaborator on {full_name}, so it will not put "
            "them on anything there. Somebody who can administer the repository has to invite "
            "them first."
        )
    if permission == READ_ONLY:
        return (
            f"{login} can read {full_name} but cannot be assigned in it: GitHub takes an "
            "assignee with write access or better. Triage counts as read here, which is "
            "GitHub's own folding rather than a rule of this bot's."
        )
    return (
        f"GitHub says {login} has {permission} access to {full_name} and will still not take "
        "them as an assignee. Those two answers should agree, so the GitHub account linked to "
        "them is worth checking."
    )


def _held(login: str, people: Iterable[Actor], *, adding: bool, asked: str) -> PeopleChange:
    """Refuse a change that has already happened, in either direction.

    Adding twice is a 422, and removing somebody who was never there is a 404 the client
    swallows and so reports as success for nothing.
    """
    on_it = any(_is(login, person) for person in people)
    if adding and on_it:
        return PeopleChange(login, f"{login} has already been {asked}.")
    if not adding and not on_it:
        return PeopleChange(login, f"{login} has not been {asked}.")
    return PeopleChange(login)


def _is(login: str, person: Actor | None) -> bool:
    """Whether these name the same account, compared the way GitHub compares logins.

    Case-insensitively: GitHub treats logins that way, and a login off a snapshot and one out of
    `user_links` need not agree on case.
    """
    return person is not None and person.login.casefold() == login.casefold()
