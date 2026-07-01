"""Add transmission_mode column to jobs table for OGC API Processes
transmission mode support.

OGC Spec: https://docs.ogc.org/is/18-062r2/18-062r2.html
Transmission modes: 'value' (inline results) or 'reference' (link to results)

Revision ID: add_transmission_mode
Revises:
Create Date: 2026-06-29

"""

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "add_transmission_mode"
down_revision = "1.0.11"
branch_labels = None
depends_on = None


def upgrade():
    """
    Add transmission_mode column to jobs table.

    This column stores how the process results should be transmitted:
        - 'value': Results transmitted inline in the response body (default,
            backward compatible)
    - 'reference': Results transmitted as a reference/link (e.g., to GeoServer layer)

    Default is 'value' to maintain backward compatibility with existing clients.
    """
    # Add column with default value 'value' to maintain backward compatibility
    op.add_column(
        "jobs",
        sa.Column(
            "transmission_mode",
            sa.String(length=20),
            nullable=False,
            server_default="value",
            comment='OGC transmission mode: "value" (inline) or "reference" (link)',
        ),
    )

    # Create check constraint to ensure only valid values
    op.create_check_constraint(
        "ck_transmission_mode_values",
        "jobs",
        "transmission_mode IN ('value', 'reference')",
    )


def downgrade():
    """
    Remove transmission_mode column and constraint.
    Reverting this migration will remove OGC transmission mode support.
    """
    op.drop_constraint("ck_transmission_mode_values", "jobs")
    op.drop_column("jobs", "transmission_mode")
