"""Index tracked items by repository and number

Comments and reviews find their item by number, because GitHub reports a pull request's issue
id in those payloads and that never matches the stored pull request id. The unique constraint
on the table leads with repository_id, so the planner was scanning every item in the repository
and filtering on the number, which grows with the repository instead of staying flat.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_tracked_items_repo_number",
        "tracked_items",
        ["repository_id", "github_object_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tracked_items_repo_number", table_name="tracked_items")
