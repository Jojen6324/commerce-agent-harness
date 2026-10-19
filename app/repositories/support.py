"""Durable inbox with idempotent submission and atomic operator transitions."""

import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import InvalidRequestError
from app.core.ids import new_id
from app.core.time import utcnow
from app.repositories.models import AuditLogRow, SupportTicketRow


def ticket_data(row: SupportTicketRow) -> dict:
    return {
        column.name: value.isoformat() if hasattr(value, "isoformat") else value
        for column in row.__table__.columns
        if column.name != "request_key"
        for value in [getattr(row, column.name)]
    }


class SupportRepository:
    def __init__(self, session):
        self.session = session

    async def submit(self, *, user_id, session_id, reason):
        key = hashlib.sha256(
            json.dumps([user_id, session_id, reason.strip()], ensure_ascii=False).encode()
        ).hexdigest()
        existing = await self.session.scalar(
            select(SupportTicketRow).where(SupportTicketRow.request_key == key)
        )
        if existing:
            return ticket_data(existing)
        row = SupportTicketRow(
            ticket_id=new_id("ticket_"),
            user_id=user_id,
            session_id=session_id,
            reason=reason.strip(),
            request_key=key,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
            await self.session.commit()
        except IntegrityError:
            existing = await self.session.scalar(
                select(SupportTicketRow).where(SupportTicketRow.request_key == key)
            )
            if existing is None:
                raise
            return ticket_data(existing)
        return ticket_data(row)

    async def transition(
        self, ticket_id, *, expected_status, status, operator_id, resolution, trace_id
    ):
        if (expected_status, status) not in {
            ("pending", "in_progress"),
            ("in_progress", "resolved"),
        }:
            raise InvalidRequestError("invalid ticket transition")
        if status == "resolved" and not resolution.strip():
            raise InvalidRequestError("resolution is required")
        statement = update(SupportTicketRow).where(
            SupportTicketRow.ticket_id == ticket_id,
            SupportTicketRow.status == expected_status,
        )
        if expected_status == "in_progress":
            statement = statement.where(SupportTicketRow.assigned_to == operator_id)
        result = await self.session.execute(
            statement.values(
                status=status,
                assigned_to=operator_id,
                resolution=resolution.strip(),
                updated_at=utcnow(),
            )
        )
        if result.rowcount != 1:
            raise InvalidRequestError(
                "ticket not found, already changed, or assigned to another operator"
            )
        self.session.add(
            AuditLogRow(
                trace_id=trace_id,
                action="support_ticket_transition",
                payload=json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "operator_id": operator_id,
                        "from": expected_status,
                        "to": status,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        await self.session.commit()
        row = await self.session.get(SupportTicketRow, ticket_id)
        return ticket_data(row)
