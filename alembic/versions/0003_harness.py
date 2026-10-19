"""Harness persistence: additive tables, preserving all existing business data."""

from alembic import op
from app.repositories.models import ActionRow, ArtifactRow, RunRow, RuntimeStateRow

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in (
        RuntimeStateRow.__table__,
        ArtifactRow.__table__,
        RunRow.__table__,
        ActionRow.__table__,
    ):
        table.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for table in (
        ActionRow.__table__,
        RunRow.__table__,
        ArtifactRow.__table__,
        RuntimeStateRow.__table__,
    ):
        table.drop(bind, checkfirst=True)
