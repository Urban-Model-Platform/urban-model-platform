"""create jobs tables

Revision ID: 0001
Revises:
Create Date: 2026-07-04

Initial migration for the UMP job persistence layer.
Creates two tables:
  - jobs                 current snapshot per job (JSONB for complex fields)
  - job_status_history   append-only status transition log

Written manually to ensure correct PostgreSQL JSONB column types and indexes.
Alembic autogenerate is used for subsequent migrations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("process_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("remote_job_id", sa.String(), nullable=True),
        sa.Column("remote_status_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_info", JSONB(), nullable=True),
        sa.Column("inputs", JSONB(), nullable=True),
        sa.Column("inputs_url", sa.String(), nullable=True),
        sa.Column("inputs_storage", sa.String(), nullable=True),
        sa.Column("inputs_size", sa.Integer(), nullable=True),
        sa.Column("inputs_checksum", sa.String(), nullable=True),
        sa.Column("links", JSONB(), nullable=True),
        sa.Column("diagnostic", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_provider", "jobs", ["provider"])
    op.create_index("idx_jobs_process_id", "jobs", ["process_id"])

    op.create_table(
        "job_status_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_jsh_job_id_seq", "job_status_history", ["job_id", "seq"])


def downgrade() -> None:
    op.drop_index("idx_jsh_job_id_seq", table_name="job_status_history")
    op.drop_table("job_status_history")
    op.drop_index("idx_jobs_process_id", table_name="jobs")
    op.drop_index("idx_jobs_provider", table_name="jobs")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_table("jobs")
