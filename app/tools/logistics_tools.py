"""Logistics tools: shipment tracking."""

from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import ToolRisk
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.deps import RepositoryBackedTool


class TrackInput(BaseModel):
    order_id: str = Field(
        min_length=3,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="订单号，如 ORD1001；不要填运单号 tracking_no",
    )


class TrackShipmentTool(RepositoryBackedTool, Tool):
    name = "track_shipment"
    description = (
        "按订单号查询物流事件与运单号（仓库/在途/签收）。"
        "何时用：快递到哪了、物流轨迹、运单号、是否签收、催物流。"
        "何时不用：问支付/金额/订单状态用 get_order；问配送政策时效用 search_knowledge；仓库截单用 mcp_warehouse_cutoff。"
        "优先使用 track_shipment 当强调物流轨迹；强调订单状态/金额则用 get_order。"
        "参数 order_id：例 ORD1001；不要把运单号（如 SF123456）填进 order_id。"
        "返回 tracking_no、city、shipped_at、events[{status,label,at,time_source}]。"
        "warehouse=支付后仓库待处理，日期来自paid_at，不是出库或发货；in_transit日期才是shipped_at。"
        "events为空仅表示无事件，不能推断已签收。"
    )
    risk = ToolRisk.READ
    input_model = TrackInput

    async def execute(self, parsed: TrackInput, context: ToolContext) -> ToolResult:
        order = await self.repository.get_visible_order(parsed.order_id, context.user_id)
        events: list[dict[str, Any]] = []
        if order.paid_at:
            events.append(
                {
                    "status": "warehouse",
                    "label": "支付后仓库待处理（非出库/发货）",
                    "time_source": "paid_at",
                    "at": order.paid_at.isoformat(),
                }
            )
        if order.shipped_at:
            events.append(
                {
                    "status": "in_transit",
                    "label": "已发货，运输中",
                    "time_source": "shipped_at",
                    "tracking_no": order.tracking_no,
                    "at": order.shipped_at.isoformat(),
                }
            )
        if order.delivered_at:
            events.append(
                {
                    "status": "delivered",
                    "label": "已签收",
                    "time_source": "delivered_at",
                    "at": order.delivered_at.isoformat(),
                }
            )
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "order_id": order.order_id,
                "tracking_no": order.tracking_no,
                "city": order.receiver_city,
                "city_meaning": "订单收货城市；不是实时包裹定位",
                "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
                "events": events,
            },
        )
