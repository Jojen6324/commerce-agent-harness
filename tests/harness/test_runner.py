import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.runner import AgentRunner, AgentSpec
from app.providers.base import LLMResponse
from app.tools.base import ToolContext


def context():
    return ToolContext(user_id="u", session_id="s", trace_id="t")


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
            return_value=LLMResponse("", tool_calls=[call()], finish_reason="tool_calls")
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
