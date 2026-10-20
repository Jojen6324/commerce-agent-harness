import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.harness.completion import policy_evidence_issue, repair_guidance
from app.harness.delegation_policy import DelegationPolicy
from app.harness.evidence import confirmed_item_observations
from app.harness.requirements import request_requirements
from app.harness.runner import AgentRunner, AgentSpec
from app.providers.base import LLMResponse
from app.repositories.models import ActionRow, OrderRow
from app.schemas.chat import ChatRequest
from app.services.container import build_container
from app.tools.base import ToolContext


@pytest.mark.parametrize(
    "query,policy",
    [
        ("ORD778899想取消还是继续等，给点建议但先别操作", True),
        ("ORD778899有运单号，是不是能确定拆成两个包裹", True),
        ("ORD778899已申请退款，requested与到账成功的区别是什么", True),
        ("ORD778899漏印了字，属于质量问题吗", True),
        ("ORD778899已经可以寄回了吗", True),
        ("ORD778899有裂纹是否不能售后", True),
        ("ORD778899快递延误能不能认定丢件并赔钱", True),
        ("ORD778899使用礼品卡的规则是什么", True),
        ("ORD778899能开票吗", True),
        ("ORD778899是否签收了", False),
        ("ORD778899退款进度如何", False),
        ("ORD778899退款进度如何，审核通过后多久原路到账", True),
    ],
)
def test_runner_and_delegation_share_evidence_requirements(query, policy):
    needs = request_requirements(query)
    assert needs.policy is policy
    assert bool(policy_evidence_issue(query)) is policy
    expected = ("orders", "knowledge") if policy else ()
    assert DelegationPolicy("required_when_eligible").decide(query).experts == expected


def test_repair_feedback_only_addresses_the_missing_policy_source():
    feedback = repair_guidance("policy_without_evidence")
    assert "只读" in feedback and "原始" in feedback
    assert "参数齐全" not in feedback and "生成pending_action" not in feedback


@pytest.mark.parametrize(
    "query,action,forbidden",
    [
        ("帮我查一下退款政策", False, False),
        ("退款政策是什么", False, False),
        ("取消要满足什么条件？", False, False),
        ("帮我退款，不要退货", True, False),
        ("先给我看看退款方案，不要执行", True, False),
        ("不要生成退款提案，只给建议", False, True),
        ("先教我怎么留证，不要生成退货单", False, True),
        ("只给建议先别操作", False, True),
    ],
)
def test_consultation_and_explicit_proposal_requests_have_different_boundaries(
    query, action, forbidden
):
    needs = request_requirements(query)
    assert needs.action_requested is action
    assert needs.proposals_forbidden is forbidden


def test_reusable_purchase_evidence_excludes_live_state_and_other_orders():
    facts = {
        "last_order": {"order_id": "ORD778899"},
        "business": {
            "ORD778899:get_order": {
                "tool": "get_order",
                "result": {
                    "order_id": "ORD778899",
                    "user_id": "u",
                    "status": "paid",
                    "items": [{"sku": "S", "customized": True}],
                    "tracking_no": None,
                },
            },
            "ORD778899:get_refund_status": {
                "tool": "get_refund_status",
                "result": {"status": "requested"},
            },
        },
    }
    observations = confirmed_item_observations(facts, "这个定制商品能退吗", "u")
    assert len(observations) == 1
    assert observations[0]["output"]["output"] == {
        "order_id": "ORD778899",
        "items": [{"sku": "S", "customized": True}],
    }
    assert observations[0]["source"] == "confirmed_session_fact"
    assert confirmed_item_observations(facts, "查ORD999900", "u") == []
    assert confirmed_item_observations(facts, "这个能退吗", "another") == []


def native(name, arguments, call_id):
    return LLMResponse(
        "",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "required_when_eligible"])
async def test_advice_only_requests_block_proposals_in_a_and_c(tmp_path, mode):
    c = await build_container(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/{mode}.db",
            agent_delegation_enabled=mode != "off",
            agent_delegation_policy=mode,
        ),
        seed=True,
    )
    try:
        c.chat_service.runner.llm = SimpleNamespace(
            generate=AsyncMock(
                side_effect=[
                    native("cancel_order", {"order_id": "ORD10003"}, "forbidden"),
                    native("get_order", {"order_id": "ORD10003"}, "read"),
                    LLMResponse("订单为paid，尚未发货。没有取消或创建提案。"),
                ]
            )
        )
        result = await c.chat_service.chat(
            ChatRequest(message="ORD10003是否已发货？暂时不要操作。", user_id="u_demo"),
            trace_id="t",
        )
        assert result.status == "completed" and result.pending_action is None
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ActionRow)) == 0
            assert (await db.get(OrderRow, "ORD10003")).status == "paid"
    finally:
        await c.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "required_when_eligible"])
async def test_item_evidence_is_reused_without_replacing_stored_live_snapshot(tmp_path, mode):
    c = await build_container(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/{mode}-reuse.db",
            agent_delegation_enabled=mode != "off",
            agent_delegation_policy=mode,
        ),
        seed=True,
    )
    try:
        c.chat_service.runner.llm = SimpleNamespace(
            generate=AsyncMock(
                side_effect=[
                    native("get_order", {"order_id": "ORD10003"}, "original"),
                    LLMResponse("纯棉T恤，数量1。"),
                    LLMResponse("这个商品是纯棉T恤，数量1 [get_order]。"),
                ]
            )
        )
        run = c.chat_service.runner.run
        c.chat_service.runner.run = AsyncMock(side_effect=run)
        first = await c.chat_service.chat(
            ChatRequest(message="ORD10003买了什么？", user_id="u_demo"),
            trace_id="t",
        )
        second = await c.chat_service.chat(
            ChatRequest(message="这个商品是什么？", user_id="u_demo", session_id=first.session_id),
            trace_id="t2",
        )
        assert second.status == "completed" and second.diagnostics["tool_calls"] == 0
        inherited = c.chat_service.runner.run.call_args.kwargs["initial_observations"]
        assert len(inherited) == 1 and inherited[0]["source"] == "confirmed_session_fact"
        assert "status" not in inherited[0]["output"]["output"]
        stored = await c.chat_service.store.facts(
            ToolContext(
                user_id="u_demo",
                session_id=first.session_id,
                trace_id="t3",
            )
        )
        assert stored["business"]["ORD10003:get_order"]["result"]["status"] == "paid"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_completion_and_output_corrections_share_one_retry_allowance():
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("..."),
                LLMResponse("候选一"),
                LLMResponse("候选二"),
            ]
        )
    )
    runner = AgentRunner(llm, SimpleNamespace(schemas=lambda _: []), Settings())
    runner.output_validator = AsyncMock(
        return_value={
            "answer": "没有通过核验",
            "status": "validation_failed",
            "failure": {"stage": "output_validation", "reason": "candidate_rejected"},
            "validation": {"status": "rejected", "attempts": [{"reason": "missing_source"}]},
        }
    )
    result = await runner.run(
        "你好",
        ToolContext(user_id="u", session_id="s", trace_id="t"),
        AgentSpec("main", frozenset()),
    )
    assert result["status"] == "validation_failed"
    assert llm.generate.await_count == 3 and runner.output_validator.await_count == 2
    assert sum(e["event"] == "output_repair" for e in result["events"]) == 1
