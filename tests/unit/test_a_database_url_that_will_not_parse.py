"""The first message an operator can get out of this process, and what it names.

`build_engine` runs before uvicorn has started, so a URL that will not parse fails here, ahead of
every check written to be helpful about a database that cannot be reached. SQLAlchemy's own words
for it are "Could not parse SQLAlchemy URL from string ''", which is accurate and names nothing to
go and change.

Blank is the shape it usually takes. `SHANNON_DATABASE_URL=` left empty in a copied `.env` reads
as a value that was set, and pydantic takes it, so nothing before this notices.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import ArgumentError

from shannon.config import Settings
from shannon.db.session import build_engine


@pytest.mark.parametrize("url", ["", "nonsense", "://", "postgres@localhost"])
def test_it_says_which_setting_to_go_and_look_at(url: str) -> None:
    with pytest.raises(ArgumentError, match="SHANNON_DATABASE_URL"):
        build_engine(url)


def test_it_shows_the_shape_a_working_one_has() -> None:
    """A message that only says the value is wrong leaves somebody guessing at the fix."""
    with pytest.raises(ArgumentError, match=r"postgresql\+asyncpg://"):
        build_engine("")


def test_the_default_still_builds() -> None:
    """Connecting to nothing, which is the point: an engine opens no socket until it is used."""
    engine = build_engine(Settings(github_webhook_secret="x").database_url.get_secret_value())

    assert engine.url.drivername == "postgresql+asyncpg"
