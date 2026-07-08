"""Add output_transmission_modes column to jobs table for per-output mode support.

This column stores the original per-output transmission modes from the execute request,
allowing UMP to correctly deliver results with the modes requested by the client
even when multiple outputs with different modes are present.

Format: JSON-serialized dict mapping output_id -> transmission mode
Example: {"output_a": "value", "output_b": "reference"}

Revision ID: add_output_transmission_modes
Revises: add_transmission_mode
Create Date: 2026-07-08

"""

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "add_output_transmission_modes"
down_revision = "add_transmission_mode"
branch_labels = None
depends_on = None


def upgrade():
    """
    Add output_transmission_modes column to jobs table.

    This column stores the original per-output transmission mode specifications
    from the OGC execute request as a JSON string:
        - Format: {"output_id": "mode", ...}
        - Modes: 'value' or 'reference'
        - Default: '{}' (empty dict for backward compatibility)

    This preserves the original client request when UMP needs to normalize
    outputs to a single forwarded mode for policy enforcement.
    """
    op.add_column(
        "jobs",
        sa.Column(
            "output_transmission_modes",
            sa.String(length=2000),
            nullable=False,
            server_default="{}",
            comment="JSON-serialized per-output transmission modes from execute request",
        ),
    )


def downgrade():
    """
    Remove output_transmission_modes column.

    After downgrade, per-output transmission mode information will be lost
    for jobs retrieved from the database. New jobs will not store this information.
    """
    op.drop_column("jobs", "output_transmission_modes")
