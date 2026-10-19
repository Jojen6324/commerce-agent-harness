"""Minimal staff inbox. Disabled without an explicitly configured admin token."""

import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import get_container
from app.core.exceptions import InvalidRequestError, UnauthorizedError
from app.repositories.models import MessageRow, SupportTicketRow
from app.repositories.support import SupportRepository, ticket_data
from app.services.container import AppContainer


async def require_support_operator(
    request: Request, container: AppContainer = Depends(get_container)
):
    token = container.settings.admin_api_token.get_secret_value()
    if not token or not hmac.compare_digest(request.headers.get("x-admin-token", ""), token):
        raise UnauthorizedError("support inbox requires an admin token")


router = APIRouter(
    prefix="/admin/support-tickets",
    tags=["support"],
    dependencies=[Depends(require_support_operator)],
)


class TicketTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_status: Literal["pending", "in_progress"]
    status: Literal["in_progress", "resolved"]
    operator_id: str = Field(min_length=1, max_length=64)
    resolution: str = Field(default="", max_length=2000)


@router.get("")
async def list_tickets(
    status: Literal["pending", "in_progress", "resolved"] | None = "pending",
    limit: int = Query(default=50, ge=1, le=100),
    container: AppContainer = Depends(get_container),
):
    statement = (
        select(SupportTicketRow)
        .order_by(SupportTicketRow.created_at, SupportTicketRow.ticket_id)
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(SupportTicketRow.status == status)
    async with container.session_factory() as db:
        return {"tickets": [ticket_data(r) for r in (await db.scalars(statement)).all()]}


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: str, container: AppContainer = Depends(get_container)):
    async with container.session_factory() as db:
        row = await db.get(SupportTicketRow, ticket_id)
        if row is None:
            raise InvalidRequestError("ticket not found")
        messages = (
            await db.scalars(
                select(MessageRow)
                .where(
                    MessageRow.session_id == row.session_id,
                )
                .order_by(MessageRow.id.desc())
                .limit(50)
            )
        ).all()
        return {
            "ticket": ticket_data(row),
            "messages": [{"role": m.role, "content": m.content} for m in reversed(messages)],
        }


@router.patch("/{ticket_id}")
async def update_ticket(
    ticket_id: str,
    payload: TicketTransition,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    async with container.session_factory() as db:
        return await SupportRepository(db).transition(
            ticket_id,
            **payload.model_dump(),
            trace_id=request.state.trace_id,
        )
