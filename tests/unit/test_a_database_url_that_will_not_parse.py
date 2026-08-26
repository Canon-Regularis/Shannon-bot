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
from sqlalchemy.pool import NullPool

from shannon.config import Settings
from shannon.db.session import build_engine, build_probe_engine

URL = "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon"


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


class TestTheEngineTheHealthProbeAsksThrough:
    """A second engine, because a pooled connection is what breaks the probe's deadline.

    The engine everything else uses pre-pings on checkout, which is right for work that must not
    be handed a connection that died in the pool and wrong for a question with a deadline: when
    the deadline cancels a pre-ping, SQLAlchemy terminates the connection, and terminating an
    asyncpg connection opens a second socket for the cancel and waits on it with nothing bounding
    it. Measured against a frozen database, the first health check after an outage began answered
    nothing for eleven minutes and every later one queued behind it, so the endpoint that exists
    to report an outage was the one thing the outage silenced.
    """

    def test_it_holds_no_connection_between_probes(self) -> None:
        probes = build_probe_engine(build_engine(URL))

        assert isinstance(probes.pool, NullPool)

    def test_it_does_not_pre_ping(self) -> None:
        probes = build_probe_engine(build_engine(URL))

        assert probes.pool._pre_ping is False

    def test_it_asks_the_same_database(self) -> None:
        engine = build_engine(URL)

        assert build_probe_engine(engine).url == engine.url

    def test_the_engine_everything_else_uses_still_pre_pings(self) -> None:
        """The probe's needs are not the application's: work handed a connection that died in
        the pool fails, and the pre-ping is what stops that."""
        assert build_engine(URL).pool._pre_ping is True
