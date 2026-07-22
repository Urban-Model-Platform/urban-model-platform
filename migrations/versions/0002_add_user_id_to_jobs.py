"""add user_id to jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("user_id", sa.String(), nullable=True))
    op.create_index("idx_jobs_user_id", "jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_jobs_user_id", table_name="jobs")
    op.drop_column("jobs", "user_id")
