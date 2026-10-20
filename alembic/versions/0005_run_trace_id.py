"""Persist request trace correlation on each harness run."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("harness_runs")}
    if "trace_id" not in columns:
        op.add_column("harness_runs", sa.Column("trace_id", sa.String(length=64), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_runs")}
    if "ix_harness_runs_trace_id" not in indexes:
        op.create_index("ix_harness_runs_trace_id", "harness_runs", ["trace_id"])


def downgrade():
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_runs")}
    if "ix_harness_runs_trace_id" in indexes:
        op.drop_index("ix_harness_runs_trace_id", table_name="harness_runs")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("harness_runs")}
    if "trace_id" in columns:
        op.drop_column("harness_runs", "trace_id")
