"""add result_crs column to jobs

The CRS the remote produces a job's outputs in, resolved at job creation from
the process's result-crs-input-field / result-crs-default. Stored separately
because large inputs are not persisted inline.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("result_crs", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "result_crs")
