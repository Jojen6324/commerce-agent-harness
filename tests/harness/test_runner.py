import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.runner import SYSTEM, AgentRunner, AgentSpec
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
    result = await AgentRunner(llm, executor, Settings()).run(
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
        result = await AgentRunner(llm, executor, Settings()).run(
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
    result = await AgentRunner(llm, executor, Settings()).run(
        "查询", context(), AgentSpec("main", frozenset(["get_order"]))
    )
    assert result["status"] == "budget_exhausted"
    assert executor.execute.await_count == 2


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
    result = await AgentRunner(llm, executor, Settings()).run(
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
    result = await AgentRunner(llm, executor, Settings()).run(
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

    result = await AgentRunner(llm, executor, Settings()).run(
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
    result = await AgentRunner(llm, executor, Settings()).run(
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
    result = await AgentRunner(
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
    result = await AgentRunner(llm, executor, Settings()).run(
        "核对", context(), AgentSpec("main", frozenset(["delegate_task"]))
    )
    assert result["status"] == "completed"
    message = json.loads(llm.generate.call_args_list[1].args[0][-1]["content"])
    assert message["summary"] == output["summary"]
    assert message["facts"] == facts and message["evidence_refs"] == ["policy_01"]
    assert message["missing_fields"] == ["quality_proof"]
    assert message["artifact_ref"] == "art_large" and "chunks" not in message
    executor.execute.assert_awaited_once()
