import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.budget import BudgetedProvider
from app.providers.base import LLMResponse
from app.repositories.models import RunRow
from app.schemas.chat import ChatRequest
from app.schemas.common import TokenUsage
from app.services.container import build_container
from app.tools.base import ToolContext


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
    assert "answer_question" not in c.chat_service.spec.tools
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
            else "policy"
            if "你是只读政策研究专家" in system
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
        if role == "policy":
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "answer_question" not in names
            assert names <= {"search_knowledge", "read_artifact"}
            if roles[role] == 1:
                return native(
                    "search_knowledge", {"query": "耳机 七天无理由 签收次日起算"}, "policy"
                )
            tool_results = [
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "tool"
            ]
            chunks = [
                chunk
                for result in tool_results
                for chunk in result.get("output", {}).get("chunks", [])
            ]
            refs = [chunks[0]["chunk_id"]] if chunks else []
            return LLMResponse(
                json.dumps(
                    {
                        "status": "sufficient" if refs else "insufficient",
                        "findings": [
                            {
                                "claim": "已查到退货起算规则。",
                                "evidence_refs": refs,
                                "applicability": ["耳机"],
                                "exceptions": [],
                            }
                        ]
                        if refs
                        else [],
                        "missing_information": [] if refs else ["policy_source"],
                        "evidence_refs": refs,
                    }
                ),
                usage=TokenUsage(total_tokens=100),
            )
        assert "delegate_task" in {tool["function"]["name"] for tool in kwargs["tools"]}
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
        async with c.session_factory() as db:
            root_run = await db.get(RunRow, response.run_id)
            assert root_run.trace_id == response.trace_id and root_run.parent_run_id is None
            for attempt in delegation["attempts"]:
                child_run = await db.get(RunRow, attempt["child_run_id"])
                assert child_run.parent_run_id == response.run_id
                assert child_run.trace_id == response.trace_id
        persisted_facts = await c.chat_service.store.facts(
            ToolContext(
                user_id="u_demo",
                session_id=response.session_id,
                trace_id=response.trace_id,
            )
        )
        policy_refs = [
            item
            for item in persisted_facts.get("evidence_refs", [])
            if item.get("source_tool") == "search_knowledge"
        ]
        assert policy_refs and policy_refs[-1]["source_trace_id"] == response.trace_id
        assert dispatches == [
            ("main", "delegate_task"),
            ("orders", "get_order"),
            ("orders", "track_shipment"),
            ("main", "delegate_task"),
            ("policy", "search_knowledge"),
            ("main", "ask_user"),
        ]
        policy_context = "\n".join(m["content"] for m in prompts["policy"])
        main_context = "\n".join(m["content"] for m in prompts["main"])
        assert "verified_observations" in policy_context and "shipped" in policy_context
        assert "fact_refs" in main_context and "child_run_id" in main_context
        assert "delegated_by" in main_context and "search_knowledge" in main_context
        assert all(not any(m.get("role") == "tool" for m in prompts[role]) for role in prompts)
    finally:
        await c.aclose()
