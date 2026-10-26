"""Novels remember whether the user renamed them, so a sync replaces only a detected title.

`novels` gains `title_locked` (false for every existing novel). Additive only; downgrade
drops the column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "novels",
        sa.Column("title_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    with op.batch_alter_table("novels") as batch:
        batch.drop_column("title_locked")
