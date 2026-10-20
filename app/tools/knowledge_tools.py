"""Knowledge tools: policy search, coupons, warranty and escalation."""

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ToolRisk
from app.core.request_scope import get_db_session
from app.repositories.support import SupportRepository
from app.retrieval.hybrid import HybridRetriever
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext


class KnowledgeSearchInput(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="政策检索问句，如「7天无理由是否含定制」；不要填 order_id",
    )


class CouponInput(BaseModel):
    user_id: str | None = Field(
        default=None,
        max_length=64,
        description="当前用户 ID，如 u_demo；不要填邮箱或手机号。省略则返回通用券规则",
    )


class WarrantyInput(BaseModel):
    sku: str = Field(
        min_length=2,
        max_length=32,
        description="商品 SKU，如 SKU-A1；不要填商品中文名",
    )


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "检索 FAQ / 退换货 / 配送等政策文档片段，用于规则类问题，不查真实订单或库存。"
        "何时用：怎么退、时效、偏远是否包邮、7天无理由是否含定制等政策，不依赖具体订单数据。"
        "何时不用：查真实订单/物流/库存/退款单；券码与叠用优先 get_coupon；有 sku 的保修摘要优先 get_warranty；客服营业时间优先 mcp_store_hours；仓库截单优先 mcp_warehouse_cutoff。"
        "优先：结构化券规则 → get_coupon；有 sku 的保修 → get_warranty；其余政策 → 本工具。"
        "参数 query：1–200 字检索问句，如「7天无理由退货是否含定制」，不要填 ORD1001。"
        "返回完整 chunks，含 document_id、标题、版本、适用类别、条件、例外和 chunk_id；不能当成某笔订单的事实。"
    )
    risk = ToolRisk.READ
    input_model = KnowledgeSearchInput

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def execute(self, parsed: KnowledgeSearchInput, context: ToolContext) -> ToolResult:
        chunks = await self._retriever.retrieve(parsed.query, top_k=4)
        return ToolResult(
            name=self.name,
            success=True,
            output={"chunks": [c.model_dump(mode="json") for c in chunks]},
        )


class GetCouponTool(Tool):
    name = "get_coupon"
    description = (
        "查询通用券目录及已索引的完整优惠券/叠加政策原文。只读，不会自动领券。"
        "何时用：有什么券、能不能叠、满减、包邮券。"
        "何时不用：查订单实付金额用 get_order；一般促销文案可用 search_knowledge，但券码与叠用规则优先本工具。"
        "优先使用 get_coupon 当问券/叠用；search_knowledge 仅作补充政策。"
        "参数 user_id 可选：当前登录用户 ID，例 u_demo；不要填邮箱（user@example.com）或手机号。不填则返回通用规则。"
        "返回 coupons、chunks和limitations；desc只是摘要，限制与叠加以chunks为准。"
        "不提供实际领取入口或账户可用资格，不能编造领取路径或推断未提及的券可叠加。"
    )
    risk = ToolRisk.READ
    input_model = CouponInput

    def __init__(self, retriever: HybridRetriever | None = None):
        self.retriever = retriever

    async def execute(self, parsed: CouponInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "chunks": [
                    c.model_dump(mode="json")
                    for c in self.retriever.document_chunks({"coupons", "promo_stacking"})
                ]
                if self.retriever
                else [],
                "limitations": {
                    "account_eligibility": "unknown",
                    "claim_entry": "not_recorded",
                    "claimed": False,
                    "summary_is_complete_policy": False,
                },
                "coupons": [
                    {"code": "NEW10", "desc": "新客满 99 减 10，不与品类券叠用"},
                    {"code": "SHIP0", "desc": "满 59 包邮，偏远地区除外"},
                ],
            },
        )


class GetWarrantyTool(Tool):
    name = "get_warranty"
    description = (
        "按 SKU 返回保修月数与免责摘要（结构化，非全文政策）。"
        "何时用：已知 sku，问这件保修多久、人为损坏管不管。"
        "何时不用：不知 sku 时用 search_knowledge 查通用保修政策；价格规格用 get_product_detail。"
        "优先：有 sku → 本工具；无 sku 问保修政策 → search_knowledge。"
        "参数 sku：例 SKU-A1，不是商品中文名或 order_id。"
        "返回 {sku, warranty_months, note}；后续回答不要承诺超出 note 的范围（如人为损坏在保）。"
    )
    risk = ToolRisk.READ
    input_model = WarrantyInput

    async def execute(self, parsed: WarrantyInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "sku": parsed.sku,
                "warranty_months": 12,
                "note": "人为损坏不在保，电子凭证见订单详情",
            },
        )


class EscalateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    reason: str = Field(
        min_length=1,
        max_length=200,
        description="转人工原因摘要，如「用户要求值班经理介入」；不要填 order_id 代替原因",
    )


class EscalateToHumanTool(Tool):
    name = "escalate_to_human"
    description = (
        "将人工服务请求保存到数据库待办，供授权工作人员受理；不是实时聊天或外部呼叫中心。"
        "这是会创建持久记录的操作，只能在用户明确要求人工/真人客服/值班经理介入后调用。"
        "如果查询与政策工具无法解决，或存在投诉升级/敏感纠纷，但用户尚未明确要求转人工，先说明原因并询问是否需要；不能自行建单。"
        "何时不用：先能用 get_order、search_knowledge 等解决时不要转；不要用它查订单、物流或政策。"
        "优先：业务可工具化 → 先调对应只读工具；用户明确要求人工后 → 本工具。"
        "参数 reason：1–200 字概括为何转人工，如「用户要求值班经理审核退款」。"
        "用户明确要求人工时无需先提供订单号，可以先入队再补充订单背景，不要代替用户申请退款。"
        "返回 {ticket_id, queue:human, status, delivery:database, connected:false}。"
        "pending表示已保存待受理，in_progress表示有工作人员受理，resolved表示已解决。"
        "不能承诺已接通、经理已接单、稍后一定联系、响应时间或退款获批。"
    )
    risk = ToolRisk.EXTERNAL
    input_model = EscalateInput

    async def execute(self, parsed: EscalateInput, context: ToolContext) -> ToolResult:
        ticket = await SupportRepository(get_db_session()).submit(
            user_id=context.user_id,
            session_id=context.session_id,
            reason=parsed.reason,
        )
        return ToolResult(
            name=self.name,
            success=True,
            risk=self.risk,
            output={
                **ticket,
                "queue": "human",
                "delivery": "database",
                "connected": False,
                "notice": "工单已持久保存，等待工作人员受理；未接通实时人工，不保证处理时间，不自动批准退款。",
            },
        )
