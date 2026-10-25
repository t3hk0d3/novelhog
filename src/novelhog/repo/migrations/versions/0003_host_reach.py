"""Host pause and throttle state, and the transient retries a task has used.

`hosts` gains `reach_after` (the host is not contacted before it), `failures` (consecutive
failures), `failing_since` and `last_failure`; `tasks` gains `retries`. Additive only; downgrade
drops the columns.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from novelhog.repo.types import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("reach_after", UTCDateTime(), nullable=True))
    op.add_column("hosts", sa.Column("failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("hosts", sa.Column("failing_since", UTCDateTime(), nullable=True))
    op.add_column("hosts", sa.Column("last_failure", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("retries", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("retries")
    with op.batch_alter_table("hosts") as batch:
        batch.drop_column("last_failure")
        batch.drop_column("failing_since")
        batch.drop_column("failures")
        batch.drop_column("reach_after")
