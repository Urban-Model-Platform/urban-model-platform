"""add response_mode and outputs_spec to jobs

These two columns capture the client's original execute-request preferences so
that Feature VIII (result storage / transmission-mode policy enforcement) can
read them at job-completion time.

  response_mode  - the client's ``response`` field: "raw" | "document"
  outputs_spec   - the verbatim ``outputs`` map from the execute body (JSONB)

Both are nullable: jobs created before this migration have NULL, which the
application treats as "no preference recorded" (safe fallback to pass-through
behaviour).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # response_mode is a short string ("raw" or "document"); TEXT is enough.
    op.add_column("jobs", sa.Column("response_mode", sa.String(), nullable=True))

    # outputs_spec is the verbatim JSON "outputs" map from the execute request.
    # JSONB lets Postgres index and query inside it if needed in the future.
    op.add_column(
        "jobs",
        sa.Column(
            "outputs_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "outputs_spec")
    op.drop_column("jobs", "response_mode")
