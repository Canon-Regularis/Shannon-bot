"""What a conditional paged read answers.

Declared apart from both the client that produces one and the board reader that consumes it,
because neither of those imports the other and neither should start to. The client knows nothing
about boards, and the board reader names what it needs as a Protocol rather than reaching for an
implementation; a shared result type belongs beside both rather than inside either.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PagedRead:
    """Every page of a list endpoint, or the news that none of it changed.

    `pages is None` means GitHub answered 304 and the copy the caller already holds is still
    current. Deliberately not an empty tuple: an empty tuple is a list with nothing in it, which
    for a board means every card was deleted. Reading one as the other would either wipe a board
    or hide one, so the two cannot share a spelling.

    `etag` is the validator to send next time, and it is None when there is nothing safe to send.
    GitHub's ETag hashes ONE response body rather than the collection behind it - `per_page=50`
    and `per_page=100` answer with different ones, page two carries its own, and a 304 carries no
    Link header to say whether a page two even exists. So a validator may only be kept for a list
    that arrived whole, in a single page. None here does not mean "GitHub sent no ETag"; it means
    "no ETag that can speak for everything", which is the only kind worth storing.
    """

    etag: str | None
    pages: tuple[object, ...] | None
