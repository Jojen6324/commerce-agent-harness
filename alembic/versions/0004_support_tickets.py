"""Durable human-service inbox; no changes to existing business tables."""

from alembic import op
from app.repositories.models import SupportTicketRow

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    SupportTicketRow.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    SupportTicketRow.__table__.drop(op.get_bind(), checkfirst=True)
