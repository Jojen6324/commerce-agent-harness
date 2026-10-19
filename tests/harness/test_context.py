import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.context import ContextBuilder
from app.harness.validation import AnswerValidator
from app.providers.base import LLMResponse
from app.providers.fake import OfflineLLMProvider


def test_compression_preserves_call_pair_current_goal_and_facts():
    calls = [
        {
            "id": "c",
            "type": "function",
            "function": {"name": "get_order", "arguments": '{"order_id":"ORD10003"}'},
        }
    ]
    messages = [
        {"role": "system", "content": "规则"},
        *({"role": "user", "content": "旧历史" * 500} for _ in range(8)),
        {
            "role": "user",
            "content": "订单ORD10003有质量问题，定制商品例外待核实",
            "current_task": True,
        },
        {"role": "assistant", "content": "", "tool_calls": calls},
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": json.dumps(
                {
                    "artifact_ref": "art1",
                    "summary": "定制质量例外待核实",
                    "facts": {"order_id": "ORD10003"},
                    "large": "x" * 5000,
                }
            ),
        },
    ]
    compressed, changed = ContextBuilder(7000).build(messages, [])
    assert changed
    assert compressed[-2]["tool_calls"][0]["id"] == compressed[-1]["tool_call_id"]
    assert "ORD10003" in json.dumps(compressed, ensure_ascii=False)
    assert any("定制商品例外" in m["content"] for m in compressed)
    assert messages[-1]["content"] != compressed[-1]["content"]


@pytest.mark.asyncio
async def test_budget_shared_across_provider_calls():
    provider = SimpleNamespace(generate=AsyncMock(return_value=LLMResponse("ok")))
    wrapped = BudgetedProvider(provider)
    token = current_budget.set(RunBudget(time.monotonic() + 10, max_models=1))
    try:
        await wrapped.generate([{"role": "user", "content": "hi"}])
        with pytest.raises(BudgetExceeded):
            await wrapped.generate([{"role": "user", "content": "retry"}])
        assert provider.generate.await_count == 1
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_invalid_citation_and_unexecuted_write_claim_rejected():
    validator = AnswerValidator(OfflineLLMProvider())
    for answer in ["可退 [nonexistent]", "退款已完成"]:
        result = await validator.validate(
            "退货", {"answer": answer, "status": "completed", "observations": []}
        )
        assert result["status"] == "validation_failed"
