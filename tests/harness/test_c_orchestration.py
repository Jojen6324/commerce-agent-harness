import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.budget import BudgetedProvider
from app.providers.base import LLMResponse
from app.schemas.chat import ChatRequest
from app.schemas.common import TokenUsage
from app.services.container import build_container


def native(name, arguments, identifier):
    return LLMResponse(
        "",
        usage=TokenUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        tool_calls=[
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_c_executes_both_experts_with_six_tools_and_passes_verified_order_to_policy(tmp_path):
    settings = Settings(
        app_env="testing",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/c.db",
        agent_delegation_policy="required_when_eligible",
    )
    c = await build_container(settings, seed=True)
    roles = Counter()
    prompts = {}
    dispatches = []
    original = c.chat_service.executor.execute

    async def audited(name, args, context, spec, **kwargs):
        dispatches.append((spec.name, name))
        return await original(name, args, context, spec, **kwargs)

    async def generate(messages, **kwargs):
        system = messages[0]["content"]
        role = (
            "orders"
            if "你是只读订单专家" in system
            else "knowledge"
            if "你是只读知识专家" in system
            else "main"
        )
        roles[role] += 1
        prompts.setdefault(role, messages)
        if role == "orders":
            if roles[role] == 1:
                return native("get_order", {"order_id": "ORD10001"}, "order")
            if roles[role] == 2:
                return native("track_shipment", {"order_id": "ORD10001"}, "shipment")
            return LLMResponse(
                json.dumps(
                    {
                        "status": "completed",
                        "missing_fields": [],
                        "summary": "ORD10001 已发货，尚未签收。",
                    }
                ),
                usage=TokenUsage(total_tokens=100),
            )
        if role == "knowledge":
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "answer_question" not in names
            if roles[role] == 1:
                return native(
                    "search_knowledge", {"query": "耳机 七天无理由 签收次日起算"}, "policy"
                )
            return LLMResponse(
                json.dumps(
                    {"status": "completed", "missing_fields": [], "summary": "已查到退货起算规则。"}
                ),
                usage=TokenUsage(total_tokens=100),
            )
        assert "delegate_task" not in {tool["function"]["name"] for tool in kwargs["tools"]}
        return native(
            "ask_user",
            {"question": "请提供实际签收日期。", "missing_fields": ["delivered_at"]},
            "clarify",
        )

    c.chat_service.executor.execute = audited
    c.chat_service.runner.llm = BudgetedProvider(
        SimpleNamespace(generate=AsyncMock(side_effect=generate))
    )
    try:
        response = await c.chat_service.chat(
            ChatRequest(
                message="核实 ORD10001 的商品及签收状态，并说明耳机的七天无理由从哪天起算",
                user_id="u_demo",
            ),
            trace_id="t",
        )
        delegation = response.diagnostics["delegation"]
        assert delegation["required_experts_complete"] is True
        assert delegation["budget_blocked"] == []
        assert response.status == "needs_input"
        assert response.diagnostics["tool_calls"] == 4
        assert dispatches == [
            ("main", "delegate_task"),
            ("orders", "get_order"),
            ("orders", "track_shipment"),
            ("main", "delegate_task"),
            ("knowledge", "search_knowledge"),
            ("main", "ask_user"),
        ]
        policy_context = "\n".join(m["content"] for m in prompts["knowledge"])
        main_context = "\n".join(m["content"] for m in prompts["main"])
        assert "verified_observations" in policy_context and "shipped" in policy_context
        assert "fact_refs" in main_context and "child_run_id" in main_context
        assert "delegated_by" in main_context and "search_knowledge" in main_context
        assert all(not any(m.get("role") == "tool" for m in prompts[role]) for role in prompts)
    finally:
        await c.aclose()
