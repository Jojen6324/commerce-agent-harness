"""Persist application-owned business runtime checkpoints."""

import sqlalchemy as sa

from alembic import op

revision = "0006_business_checkpoints"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "harness_checkpoints",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("node", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("session_id", sa.String(64), nullable=False, index=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("harness_checkpoints")
