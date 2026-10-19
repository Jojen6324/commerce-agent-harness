"""Customer-scoped read access to the durable staff inbox."""

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.exceptions import InvalidRequestError
from app.core.request_scope import get_db_session
from app.repositories.models import SupportTicketRow
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext


class TicketInput(BaseModel):
    ticket_id: str = Field(
        min_length=1, max_length=64, description="已保存工单的真实ticket_id，不是订单号；不可猜测"
    )


class GetSupportTicketTool(Tool):
    name = "get_support_ticket"
    description = "读取当前用户当前会话已保存的人工工单状态和工作人员处理答复。ticket_id须来自已有回执。pending待受理、in_progress处理中、resolved已解决；不代表接通实时人工或退款到账。"
    input_model = TicketInput

    async def execute(self, parsed: TicketInput, context: ToolContext) -> ToolResult:
        row = await get_db_session().scalar(
            select(SupportTicketRow).where(
                SupportTicketRow.ticket_id == parsed.ticket_id,
                SupportTicketRow.user_id == context.user_id,
                SupportTicketRow.session_id == context.session_id,
            )
        )
        if row is None:
            raise InvalidRequestError("support ticket not found in this conversation")
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "ticket_id": row.ticket_id,
                "status": row.status,
                "reason": row.reason,
                "resolution": row.resolution,
                "connected": False,
            },
        )
