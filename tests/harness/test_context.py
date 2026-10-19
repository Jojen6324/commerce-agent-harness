import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.context import ContextBuilder
from app.harness.validation import AnswerValidator
from app.providers.base import LLMResponse
from app.providers.fake import OfflineLLMProvider


def test_default_agent_budgets_are_32k_context_and_80k_run():
    assert Settings().agent_context_tokens == 32_000
    assert Settings().agent_total_tokens == 80_000
    assert ContextBuilder().limit == 32_000
    assert BudgetedProvider(OfflineLLMProvider()).context_tokens == 32_000
    assert RunBudget(time.monotonic() + 10).max_tokens == 80_000


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


def test_context_overflow_reports_estimate_and_limit():
    builder = ContextBuilder(limit=1, reserve=0)
    with pytest.raises(BudgetExceeded) as raised:
        builder.build([{"role": "system", "content": "long enough to exceed the limit"}], [])

    assert raised.value.context_tokens == builder.estimate(
        [{"role": "system", "content": "long enough to exceed the limit"}], []
    )
    assert raised.value.context_limit_tokens == 1
    assert raised.value.message_count == 1
    assert raised.value.tool_count == 0


def test_run_token_budget_is_distinct_from_model_call_limit():
    budget = RunBudget(time.monotonic() + 10, max_models=24, max_tokens=10)
    with pytest.raises(BudgetExceeded, match="run_token_budget") as raised:
        budget.reserve_model(11)

    assert raised.value.budget_scope == "run_tokens"
    assert raised.value.requested_tokens == 11
    assert raised.value.projected_tokens == 11
    assert raised.value.max_total_tokens == 10


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
