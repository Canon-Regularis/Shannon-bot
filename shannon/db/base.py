from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import CursorResult, DateTime, Enum, Interval, MetaData, func, literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import cast

# Explicit naming so Alembic autogenerate produces stable constraint names across revisions.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _values_of(python_enum: type[StrEnum]) -> list[str]:
    """What SQLAlchemy writes, which is the value rather than the member name."""
    return [member.value for member in python_enum]


def varchar_enum(python_enum: type[StrEnum], name: str) -> Enum:
    """Store an enum as VARCHAR rather than a native PostgreSQL type.

    A native enum needs an ALTER TYPE migration every time a later stage adds a value, and two
    stages are still to come. The cost is that nothing constrains the column: `create_constraint`
    defaults to False, so no CHECK is emitted and the database will take any string that fits.
    The application is the only thing enforcing these values.

    Worth knowing what that costs if something else ever writes one. SQLAlchemy raises
    `LookupError` on the way out, for the whole query rather than the one row, so a single value
    the code does not recognise makes every read of that table fail rather than that item
    misbehave. Reached by editing the database by hand, or by rolling back to a version whose
    enum is missing a value a newer one wrote. Adding a value is safe in both directions; taking
    one away needs the rows carrying it moved first.

    Here rather than in `models.py` because it is a decision about how this schema renders, which
    is what this module is for, and `NAMING_CONVENTION` above is the same kind of decision made
    for the same reason.
    """
    return Enum(
        python_enum,
        name=name,
        native_enum=False,
        length=32,
        values_callable=_values_of,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def interval(value: timedelta) -> ColumnElement[timedelta]:
    """A timedelta as something the database can add to a timestamp."""
    return cast(literal(value), Interval)


# `AsyncSession.execute` is annotated `Result[Any]`, where the synchronous `Session`
# equivalent is properly overloaded. This is the one place that gap is absorbed.
async def rows_changed(session: AsyncSession, statement: UpdateBase) -> int:
    """How many rows an INSERT, UPDATE or DELETE touched.

    Only `CursorResult` carries `rowcount`, and a DML statement always produces one, but
    `AsyncSession.execute` promises only `Result`. Asserted rather than tested with an `if`,
    which would be an arm no test could reach under a hundred per cent branch floor. An
    always-true assert is a plain statement to coverage.py, which has no handler for it.

    `scalar` has the same gap and no helper, because absorbing it takes a declared local and
    that reads better at the call site, where the model's own name can be written.
    """
    result = await session.execute(statement)
    assert isinstance(result, CursorResult)
    return int(result.rowcount)
