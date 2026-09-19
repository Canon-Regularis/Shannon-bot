"""Keep who a captured message tagged.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-19

Issue #121. A tag in a logged thread reached GitHub as somebody's Discord name, because
`clean_content` turns `<@123>` into `@DisplayName` before anything of ours sees the message and the
id, which is the only thing `/link` knows a person by, was gone. Capture keeps the id now, and this
is where it is kept along with the name each person had when it was said.

On the row rather than in a table of its own. These rows live minutes and are deleted whole once
the comment carrying them lands; the map is never queried by the ids in it and never joined from
the other side, so a table would buy a second cascade and a join on every flush and nothing else.

No backfill. Rows written before this are messages whose ids `clean_content` had already destroyed,
so an empty object is the truthful answer for them and they publish exactly as they did.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "logged_messages",
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("logged_messages", "mentions")
