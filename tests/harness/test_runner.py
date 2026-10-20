import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.agentic_core import SYSTEM, AgentSpec, BoundedRole
from app.harness.budget import RunBudget, current_budget
from app.prompts.evidence_rules import EVIDENCE_RULES
from app.providers.base import LLMResponse
from app.tools.base import ToolContext


def context():
    return ToolContext(user_id="u", session_id="s", trace_id="t")


def test_main_agent_uses_shared_evidence_contract():
    assert SYSTEM.endswith(EVIDENCE_RULES)


def call(name="get_order", args=None, call_id="call1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {"order_id": "ORD1"})},
    }


@pytest.mark.asyncio
async def test_tool_result_pairing_and_no_classifier():
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("", tool_calls=[call()], finish_reason="tool_calls"),
                LLMResponse("已查询"),
            ]
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(return_value={"success": True, "output": {"status": "paid"}}),
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "订单状态", context(), AgentSpec("main", frozenset(["get_order"]))
    )
    assert result["answer"] == "已查询"
    assert llm.generate.await_count == 2
    second = llm.generate.call_args_list[1].args[0]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call1"
    executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_truncated_or_malformed_calls_never_execute():
    for response in [
        LLMResponse("", tool_calls=[call()], finish_reason="length"),
        LLMResponse(
            "",
            tool_calls=[
                {**call(), "function": {"name": "cancel_order", "arguments": '{"order_id":'}}
            ],
        ),
    ]:
        llm = SimpleNamespace(generate=AsyncMock(return_value=response))
        executor = SimpleNamespace(schemas=lambda _: [], execute=AsyncMock())
        result = await BoundedRole(llm, executor, Settings()).run(
            "取消", context(), AgentSpec("main", frozenset())
        )
        assert result["status"] == "failed"
        executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_calls_stop_without_progress():
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("", tool_calls=[call(call_id=f"c{i}")], finish_reason="tool_calls")
                for i in range(3)
            ]
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [], execute=AsyncMock(return_value={"success": True, "output": {}})
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "查询", context(), AgentSpec("main", frozenset(["get_order"]))
    )
    assert result["status"] == "budget_exhausted"
    assert executor.execute.await_count == 2


@pytest.mark.asyncio
async def test_tool_budget_stop_keeps_budget_details_without_duplicate_event_fields():
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse("", tool_calls=[call()], finish_reason="tool_calls")
        )
    )

    async def reserve_tool(*_args, **_kwargs):
        current_budget.get().reserve_tool()

    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(side_effect=reserve_tool),
    )
    token = current_budget.set(RunBudget(deadline=time.monotonic() + 60, max_tools=0))
    try:
        result = await BoundedRole(llm, executor, Settings()).run(
            "查询", context(), AgentSpec("main", frozenset(["get_order"]))
        )
    finally:
        current_budget.reset(token)

    assert result["status"] == "budget_exhausted"
    stopped = next(event for event in result["events"] if event["event"] == "stopped")
    assert stopped["reason"] == "tool_budget"
    assert stopped["tool_calls"] == 0
    assert stopped["tool_call_limit"] == 0


@pytest.mark.asyncio
async def test_parameter_error_can_be_corrected_with_matched_result():
    from app.core.exceptions import InvalidRequestError

    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    tool_calls=[call(args={"wrong": "x"}, call_id="bad")],
                    finish_reason="tool_calls",
                ),
                LLMResponse("", tool_calls=[call(call_id="fixed")], finish_reason="tool_calls"),
                LLMResponse("查询完成"),
            ]
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(
            side_effect=[
                InvalidRequestError("invalid arguments for get_order"),
                {"success": True, "output": {"status": "paid"}},
            ]
        ),
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "查询", context(), AgentSpec("main", frozenset(["get_order"]))
    )
    assert result["status"] == "completed" and executor.execute.await_count == 2
    repaired = llm.generate.call_args_list[1].args[0][-1]
    assert repaired["tool_call_id"] == "bad" and "invalid arguments" in repaired["content"]


@pytest.mark.asyncio
async def test_explicit_clarification_stops_before_business_tool():
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                "",
                tool_calls=[
                    call("ask_user", {"question": "请提供订单号", "missing_fields": ["order_id"]})
                ],
                finish_reason="tool_calls",
            )
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(
            return_value={
                "status": "needs_input",
                "question": "请提供订单号",
                "missing_fields": ["order_id"],
            }
        ),
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "帮我退货", context(), AgentSpec("main", frozenset(["ask_user"]))
    )
    assert result["status"] == "needs_input" and result["answer"] == "请提供订单号"
    assert any(e["event"] == "needs_input" for e in result["events"])


@pytest.mark.asyncio
async def test_structured_missing_input_keeps_answer_and_skips_prose_completion_guard():
    question = "请提供订单号。有了订单号后我才能生成退货申请提案。"
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                "退货需先核对订单。",
                tool_calls=[
                    call(
                        "ask_user",
                        {"question": question, "missing_fields": ["order_id"]},
                    )
                ],
                finish_reason="tool_calls",
            )
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(
            return_value={
                "status": "needs_input",
                "question": question,
                "missing_fields": ["order_id"],
            }
        ),
    )

    result = await BoundedRole(llm, executor, Settings()).run(
        "查询订单", context(), AgentSpec("main", frozenset(["ask_user"]))
    )

    assert result["status"] == "needs_input"
    assert "退货需先核对订单。" in result["answer"]
    assert question in result["answer"]
    assert llm.generate.await_count == 1


@pytest.mark.asyncio
async def test_missing_input_never_revives_history_or_rejected_answer():
    question = "请提供订单号"
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("我查一下相关订单。"),
                LLMResponse(
                    "",
                    tool_calls=[
                        call(
                            "ask_user",
                            {
                                "question": question,
                                "missing_fields": ["order_id"],
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                ),
            ]
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(
            return_value={
                "status": "needs_input",
                "question": question,
                "missing_fields": ["order_id"],
            }
        ),
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "查询订单",
        context(),
        AgentSpec("main", frozenset(["ask_user"])),
        history=[SimpleNamespace(role="assistant", content="旧答复：退款已完成")],
    )
    assert result["status"] == "needs_input"
    assert result["answer"] == question


@pytest.mark.asyncio
async def test_clarification_delivers_only_explicit_current_partial_answer():
    from app.harness.executor import ToolExecutor

    partial = "当前已核实的信息。"
    fields = {"answer": partial, "question": "请提供订单号", "missing_fields": ["order_id"]}
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                "",
                tool_calls=[call("ask_user", fields)],
                finish_reason="tool_calls",
            )
        )
    )
    result = await BoundedRole(
        llm, ToolExecutor(SimpleNamespace(all=lambda: []), None), Settings()
    ).run(
        "查询订单",
        context(),
        AgentSpec("main", frozenset(["ask_user"])),
    )
    assert result["status"] == "needs_input"
    assert partial in result["answer"]
    assert fields["question"] in result["answer"]


@pytest.mark.asyncio
async def test_externalized_specialist_result_keeps_summary_facts_and_evidence():
    facts = [{"tool": "get_order", "result": {"order_id": "ORD1", "status": "delivered"}}]
    output = {
        "status": "completed",
        "summary": "定制商品质量问题例外须核对举证和品类，不能承诺退款。",
        "facts": facts,
        "evidence_refs": ["policy_01"],
        "missing_fields": ["quality_proof"],
        "chunks": [{"chunk_id": "policy_01", "content": "原文" * 3000}],
    }
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    tool_calls=[call("delegate_task", {"agent": "knowledge", "goal": "核对政策"})],
                    finish_reason="tool_calls",
                ),
                LLMResponse("已整理专家调查，尚需质量凭证。"),
            ]
        )
    )
    artifacts = SimpleNamespace(put=AsyncMock(return_value="art_large"), record=AsyncMock())
    executor = SimpleNamespace(
        schemas=lambda _: [], execute=AsyncMock(return_value=output), artifacts=artifacts
    )
    result = await BoundedRole(llm, executor, Settings()).run(
        "核对", context(), AgentSpec("main", frozenset(["delegate_task"]))
    )
    assert result["status"] == "completed"
    message = json.loads(llm.generate.call_args_list[1].args[0][-1]["content"])
    assert message["summary"] == output["summary"]
    assert message["facts"] == facts and message["evidence_refs"] == ["policy_01"]
    assert message["missing_fields"] == ["quality_proof"]
    assert message["artifact_ref"] == "art_large" and "chunks" not in message
    executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_large_artifact_read_is_compacted_with_original_reference_and_no_copy():
    from app.config.settings import Settings
    from app.harness.agentic_core import AgentSpec, BoundedRole

    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    tool_calls=[call("read_artifact", {"ref": "art_original"})],
                    finish_reason="tool_calls",
                ),
                LLMResponse("已核对原始材料。"),
            ]
        )
    )
    artifacts = SimpleNamespace(put=AsyncMock(return_value="unused_copy"), record=AsyncMock())
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(return_value={"payload": "x" * 18_000}),
        artifacts=artifacts,
    )
    result = await BoundedRole(llm, executor, Settings(agent_context_tokens=6000)).run(
        "检查原始证据",
        context(),
        AgentSpec("faithfulness", frozenset({"read_artifact"}), max_steps=2),
    )

    assert result["status"] == "completed"
    tool_message = next(
        message for message in llm.generate.call_args_list[1].args[0] if message["role"] == "tool"
    )
    compacted = json.loads(tool_message["content"])
    assert compacted["artifact_ref"] == "art_original"
    assert "payload" not in compacted
    assert artifacts.put.await_count == 0


@pytest.mark.asyncio
async def test_preloaded_expert_observations_reach_main_prompt_and_result():
    observation = {
        "name": "get_order",
        "arguments": {"order_id": "ORD10001"},
        "call_id": "child_call",
        "delegated_by": "orders",
        "output": {"success": True, "output": {"order_id": "ORD10001", "status": "delivered"}},
    }
    seen = {}

    async def generate(messages, **kwargs):
        seen["messages"] = messages
        return LLMResponse("订单已签收。")

    executor = SimpleNamespace(schemas=lambda _: [], execute=AsyncMock())
    result = await BoundedRole(SimpleNamespace(generate=generate), executor, Settings()).run(
        "查询 ORD10001 当前状态",
        context(),
        AgentSpec("main", frozenset()),
        initial_context={
            "required_experts": ["orders"],
            "experts": {"orders": {"status": "completed"}},
        },
        initial_observations=[observation],
    )

    assert result["status"] == "completed"
    assert result["observations"] == [observation]
    assert "ORD10001" in "\n".join(message["content"] for message in seen["messages"])


@pytest.mark.asyncio
async def test_bounded_expert_finalizes_existing_observations_without_another_tool_attempt():
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                '{"status":"incomplete","missing_fields":["delivered_at"],"summary":"缺少签收时间"}'
            )
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [{"type": "function", "function": {"name": "get_order"}}],
        execute=AsyncMock(),
    )
    budget = RunBudget(time.monotonic() + 60, max_tools=2, protected_tools=1, tools=1)
    token = current_budget.set(budget)
    try:
        result = await BoundedRole(llm, executor, Settings()).run(
            "核实签收",
            context(),
            AgentSpec("orders", frozenset({"get_order"}), finalize_on_tool_limit=True),
        )
    finally:
        current_budget.reset(token)
    assert result["status"] == "completed"
    assert llm.generate.call_args.kwargs["tools"] == []
    assert any(e["event"] == "expert_finalizing" for e in result["events"])
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_model_call_receives_current_remaining_shared_budget():
    from app.harness.budget import BudgetedProvider
    from app.schemas.common import TokenUsage

    response1 = LLMResponse(
        "", usage=TokenUsage(total_tokens=100), tool_calls=[call()], finish_reason="tool_calls"
    )
    response2 = LLMResponse("查询完成", usage=TokenUsage(total_tokens=100))
    model = SimpleNamespace(generate=AsyncMock(side_effect=[response1, response2]))
    budget = RunBudget(
        time.monotonic() + 60,
        max_models=6,
        max_tools=6,
        protected_models=1,
        protected_tools=2,
        protected_tokens=16000,
    )

    async def execute(*_args, **_kwargs):
        budget.reserve_tool()
        return {"success": True, "output": {"status": "paid"}}

    executor = SimpleNamespace(schemas=lambda _: [], execute=execute)
    token = current_budget.set(budget)
    try:
        result = await BoundedRole(BudgetedProvider(model), executor, Settings()).run(
            "查询订单", context(), AgentSpec("main", frozenset({"get_order"}))
        )
    finally:
        current_budget.reset(token)
    assert result["status"] == "completed"
    snapshots = [e for e in result["events"] if e["event"] == "budget_snapshot"]
    assert [e["tool_calls_remaining"] for e in snapshots] == [4, 3]
    assert [e["model_calls_remaining_including_current"] for e in snapshots] == [5, 4]
    assert [e["tokens_remaining"] for e in snapshots] == [64000, 63900]
    for invocation, snapshot in zip(model.generate.call_args_list, snapshots, strict=True):
        hints = [
            m["content"]
            for m in invocation.args[0]
            if "服务器计算的本次调用前剩余额度" in m.get("content", "")
        ]
        assert len(hints) == 1
        assert f'"tool_calls_remaining": {snapshot["tool_calls_remaining"]}' in hints[0]
