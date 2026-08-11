"""add stored_outputs column to jobs for V-10 result-storage references

This makes stored result references visible to clients. When an
output is written to ldproxy, UMP records a small mapping per output:

    output_id -> {collection_id, collection_url, items_url}

so that GET /jobs/{id}/results can overlay the stored href reference over the
(otherwise inline) remote value. The mapping lives in a dedicated JSONB column
rather than inside status_info so it is queryable and cannot be clobbered by a
remote status refresh that overwrites the status_info blob.

JSONB is used so the inner shape can evolve without further migrations; only
the column itself needs this one migration.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "stored_outputs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "stored_outputs")
