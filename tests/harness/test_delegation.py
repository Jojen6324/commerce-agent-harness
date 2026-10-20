"""Business responsibility, evidence transfer and deterministic side-effect boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.core.exceptions import ToolPermissionError
from app.harness.budget import BudgetedProvider
from app.harness.telemetry import active_role
from app.providers.base import LLMResponse
from app.repositories.models import ActionRow, ReturnRow, RunRow
from app.schemas.chat import ChatRequest
from app.schemas.common import TokenUsage
from app.services.container import build_container
from app.tools.base import ToolContext


def native(name, arguments, identifier):
    return LLMResponse(
        "",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        tool_calls=[
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
        finish_reason="tool_calls",
    )


def final(text):
    return LLMResponse(
        text, usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    )


async def container(tmp_path, generate=None, **overrides):
    settings = Settings(
        app_env="testing",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/business.db",
        max_tool_calls=14,
        agent_model_calls=32,
        agent_total_tokens=128000,
        agent_delegation_policy="auto",
        **overrides,
    )
    c = await build_container(settings, seed=True)
    if generate:
        c.chat_service.runner.llm = BudgetedProvider(SimpleNamespace(generate=generate))
    return c


@pytest.mark.asyncio
async def test_aftersales_independently_checks_order_and_policy_and_handoff_is_compact(tmp_path):
    counts, captured = {}, {}
    ref = None

    async def generate(messages, **kwargs):
        nonlocal ref
        role = active_role.get()
        counts[role] = counts.get(role, 0) + 1
        if role == "main":
            if counts[role] == 1:
                return native(
                    "delegate_task",
                    {"agent": "aftersales", "goal": "查询ORD10002退货规则，只咨询不要操作"},
                    "m1",
                )
            captured["main_after"] = json.dumps(messages, ensure_ascii=False)
            return final(f"需要核对适用品类及签收日期，尚未申请退货。[{ref}]")
        assert role == "aftersales"
        if counts[role] == 1:
            names = {x["function"]["name"] for x in kwargs["tools"]}
            assert {"get_order", "search_knowledge", "create_return_request"} <= names
            assert "delegate_task" not in names
            return native("get_order", {"order_id": "ORD10002"}, "a1")
        if counts[role] == 2:
            return native("search_knowledge", {"query": "耳机 退货 七天无理由 条件"}, "a2")
        raw = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])
        chunks = raw.get("output", {}).get("chunks", raw.get("evidence", []))
        ref = chunks[0]["chunk_id"]
        return final(f"已查询订单与退货条款；需核对品类与签收日期，未申请。[{ref}]")

    c = await container(tmp_path, generate)
    try:
        r = await c.chat_service.chat(
            ChatRequest(message="ORD10002退货有什么条件，只咨询不要操作", user_id="u_demo"),
            trace_id="trace",
        )
        assert r.status == "completed", r.model_dump()
        assert set(counts) == {"main", "aftersales"}
        assert counts["main"] == 2 and counts["aftersales"] == 3
        assert r.citations and r.citations[0].chunk_id == ref
        assert '"_observations"' not in captured["main_after"]
        assert '"chunks"' not in captured["main_after"]
        assert r.diagnostics["delegation"]["delegate_calls"] == 1
        assert r.token_usage.total_tokens == 600
        assert not any(e.get("agent") == "critic" for e in r.events)
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ActionRow)) == 0
            children = (
                await db.scalars(select(RunRow).where(RunRow.parent_run_id == r.run_id))
            ).all()
            assert len(children) == 1 and children[0].trace_id == "trace"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_simple_task_has_no_forced_expert_or_critic(tmp_path):
    generate = AsyncMock(return_value=final("你好，我可以帮你。"))
    c = await container(tmp_path, generate)
    try:
        r = await c.chat_service.chat(ChatRequest(message="你好", user_id="u_demo"), trace_id="t")
        assert r.status == "completed"
        assert generate.await_count == 1
        assert r.diagnostics["delegation"]["delegate_calls"] == 0
        assert not any(e.get("agent") != "main" for e in r.events)
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_specialists_share_read_tools_but_cannot_nest_or_cross_write_scope(tmp_path):
    c = await container(tmp_path)
    try:
        specs = c.chat_service.executor.delegate.specs
        assert set(specs) == {"shopping", "order", "aftersales"}
        assert "get_order" in specs["aftersales"].tools
        assert "search_knowledge" in specs["order"].tools
        assert "cancel_order" in specs["order"].tools
        assert "create_return_request" in specs["aftersales"].tools
        ctx = ToolContext(user_id="u_demo", session_id="s", trace_id="t")
        for spec in specs.values():
            assert "delegate_task" not in spec.tools
            with pytest.raises(ToolPermissionError):
                await c.chat_service.executor.execute(
                    "delegate_task", {"agent": "shopping", "goal": "查询"}, ctx, spec
                )
        with pytest.raises(ToolPermissionError):
            await c.chat_service.executor.execute(
                "create_refund_request",
                {"order_id": "ORD10002", "reason": "裂纹"},
                ctx,
                specs["shopping"],
            )
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_clarification_continues_case_and_confirmation_is_idempotent(tmp_path):
    messages_seen = []

    async def generate(messages, **kwargs):
        messages_seen.append(json.dumps(messages, ensure_ascii=False))
        if len(messages_seen) == 1:
            return native(
                "ask_user", {"question": "退货原因是什么？", "missing_fields": ["reason"]}, "q1"
            )
        return native(
            "create_return_request", {"order_id": "ORD10002", "reason": "尺码不合适"}, "q2"
        )

    c = await container(tmp_path, generate)
    try:
        r1 = await c.chat_service.chat(
            ChatRequest(message="帮我退货ORD10002", user_id="u_demo"), trace_id="t1"
        )
        assert r1.status == "needs_input"
        case_id = r1.diagnostics["business_case"]["case_id"]
        r2 = await c.chat_service.chat(
            ChatRequest(message="尺码不合适", user_id="u_demo", session_id=r1.session_id),
            trace_id="t2",
        )
        assert r2.status == "awaiting_confirmation"
        assert r2.diagnostics["business_case"]["case_id"] == case_id
        assert "帮我退货ORD10002" in messages_seen[1]
        request = ChatRequest(
            message="确认",
            user_id="u_demo",
            session_id=r1.session_id,
            confirm_action_id=r2.pending_action.action_id,
        )
        r3 = await c.chat_service.chat(request, trace_id="t3")
        r4 = await c.chat_service.chat(request, trace_id="t4")
        assert r3.answer == r4.answer and "尚不代表退款完成" in r3.answer
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ReturnRow)) == 1
        assert len(messages_seen) == 2
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_parallel_readonly_experts_share_budget_and_keep_trace_parents(tmp_path):
    calls = {}

    async def generate(messages, **kwargs):
        role = active_role.get()
        calls[role] = calls.get(role, 0) + 1
        await asyncio.sleep(0)
        if role == "main" and calls[role] == 1:
            a = native(
                "delegate_task",
                {"agent": "shopping", "goal": "查询耳机商品", "independent": True},
                "p1",
            )
            b = native(
                "delegate_task",
                {"agent": "order", "goal": "查询ORD10002状态", "independent": True},
                "p2",
            )
            a.tool_calls.extend(b.tool_calls)
            return a
        if role == "main":
            return final("已核对订单与商品；未执行任何操作。")
        assert not any(
            x["function"]["name"] in {"cancel_order", "create_return_request", "escalate_to_human"}
            for x in kwargs["tools"]
        )
        if calls[role] == 1:
            return (
                native("get_order", {"order_id": "ORD10002"}, role + "-read")
                if role == "order"
                else native("search_products", {"keyword": "耳机"}, role + "-read")
            )
        return final("已核实本次只读查询结果。")

    c = await container(tmp_path, generate)
    try:
        r = await c.chat_service.chat(
            ChatRequest(message="查询ORD10002订单状态及耳机商品，只查询", user_id="u_demo"),
            trace_id="parallel",
        )
        assert r.status == "completed", r.model_dump()
        assert calls == {"main": 2, "shopping": 2, "order": 2}
        assert r.token_usage.total_tokens == 720
        assert r.diagnostics["model_calls"] == 6
        children = {
            e["run_id"]
            for e in r.events
            if e.get("agent") in {"shopping", "order"} and e["event"] == "agent_started"
        }
        assert len(children) == 2
        assert all(e["parent_run_id"] == r.run_id for e in r.events if e.get("run_id") in children)
        ledger = r.diagnostics["execution_ledger"]["calls"]
        assert {x["role"] for x in ledger if x["kind"] == "model"} == {"main", "shopping", "order"}
    finally:
        await c.aclose()
