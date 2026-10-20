"""Business responsibilities, derived only from the real registered catalog."""

from enum import Enum

from app.harness.agentic_core import SYSTEM, AgentSpec


class BusinessMode(str, Enum):
    OFF = "off"
    AUTO = "auto"


SHARED = {"search_knowledge", "read_artifact", "ask_user", "get_support_ticket"}
PRODUCT = {
    "search_products",
    "get_product_detail",
    "check_inventory",
    "recommend_products",
    "get_coupon",
    "get_warranty",
}
ORDER = {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
ROLE_TOOLS = {
    "shopping": SHARED | PRODUCT,
    "order": SHARED | PRODUCT | ORDER | {"cancel_order", "escalate_to_human"},
    "aftersales": SHARED
    | PRODUCT
    | ORDER
    | {"create_return_request", "create_refund_request", "escalate_to_human"},
}
RESPONSIBILITY = {
    "shopping": "购买前咨询：搜索商品、比较规格、库存及推荐。",
    "order": "订单查询、物流配送问题与取消未发货订单。",
    "aftersales": "退货退款咨询、资格和路径核对、退款进度、准备退货退款申请。可直接查询订单、商品和政策。",
}
MAIN_PROMPT = (
    SYSTEM
    + """\n你是 Main。简单回答和单项查询直接处理；按业务责任自主决定是否委派 shopping、order、aftersales。
复杂领域任务可委派一个专家；跨业务任务保留所有子目标，有依赖则顺序，独立只读子任务可并行。
没有强制 Policy 预检。不要委派纯技术步骤，不要重复执行专家已经完成的查证。
专家 completed 只表示子任务完成，不表示已执行写入、审批通过或退款到账。
专家不支持的业务不得虚构；当前没有直接换货办理接口。专家反馈是工作结果，业务事实仍以真实工具证据为准。
业务 case 的 missing_fields 和当前负责角色供续办参考；用户改变目标或取消时更新目标，不锁死旧任务。
"""
)


def specialist_specs(registry, settings):
    available = set(registry.names()) | {"read_artifact", "ask_user"}
    return {
        role: AgentSpec(
            role,
            frozenset(names & available),
            max_steps=settings.agent_specialist_steps,
            finalize_on_tool_limit=True,
            max_tokens=settings.agent_specialist_tokens,
            max_models=settings.agent_specialist_model_calls,
            max_tools=settings.agent_specialist_tool_calls,
            prompt=SYSTEM
            + "\n你是业务专家 "
            + role
            + "："
            + RESPONSIBILITY[role]
            + "\n只处理 Main 分配的当前业务任务，不互相委派，不创建下一层 Agent。"
            + "必要时直接使用共享只读工具。缺字段用 ask_user；写操作只生成待确认方案。"
            + "避免重复查询；不要把 completed 当成业务已执行。没有换货接口，只能说明路径。",
        )
        for role, names in ROLE_TOOLS.items()
    }
