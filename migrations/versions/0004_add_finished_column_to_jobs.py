"""add finished column to jobs for efficient cleanup queries

The `finished` timestamp previously lived only inside the `status_info` JSONB
blob. Feature V's cleanup service (V-9) needs to filter jobs by "finished
before cutoff X", split by two different retention rules (anonymous vs.
authenticated). Doing that against JSONB text extraction is both fragile
(depends on the exact key/serialisation format) and unindexed (full table
scan on every cleanup run). A dedicated, indexed column is the robust choice.

The column is populated from the existing status_info JSONB for all rows that
already have a 'finished' value, so historical jobs are immediately eligible
for cleanup evaluation without waiting for their next status update.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("finished", sa.DateTime(timezone=True), nullable=True)
    )
    # Backfill from the existing JSONB snapshot so historical jobs are
    # immediately visible to the cleanup query, not just newly-updated ones.
    op.execute(
        """
        UPDATE jobs
        SET finished = (status_info->>'finished')::timestamptz
        WHERE status_info->>'finished' IS NOT NULL
        """
    )
    op.create_index("ix_jobs_finished", "jobs", ["finished"])


def downgrade() -> None:
    op.drop_index("ix_jobs_finished", table_name="jobs")
    op.drop_column("jobs", "finished")
