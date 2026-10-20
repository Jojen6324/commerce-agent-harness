"""Authored migration checks; execute after the user enables debugging."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.business_case import BusinessCases
from app.harness.runtime import AgentRuntime, RuntimeNode
from app.providers.base import LLMResponse
from app.repositories.models import AuditLogRow
from app.schemas.chat import ChatRequest
from app.schemas.common import TokenUsage
from app.services.container import build_container
from app.tools.base import ToolContext


@pytest.mark.asyncio
async def test_parallel_calls_reserve_before_await_and_cannot_overspend():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return LLMResponse("ok", usage=TokenUsage(total_tokens=200))

    provider = BudgetedProvider(SimpleNamespace(generate=generate))
    budget = RunBudget(time.monotonic() + 30, max_tokens=1000)
    token = current_budget.set(budget)
    try:
        first = asyncio.create_task(
            provider.generate([{"role": "user", "content": "hi"}], max_tokens=600)
        )
        await entered.wait()
        with pytest.raises(BudgetExceeded, match="run_token_budget"):
            await provider.generate([{"role": "user", "content": "hi"}], max_tokens=600)
        release.set()
        await first
        assert budget.models == 1 and budget.usage.total_tokens == 200
        assert budget.reserved_tokens == 0
    finally:
        release.set()
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_checkpoint_restores_consumed_budget_and_reconciles_committed_action(tmp_path):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/checkpoint.db"),
        seed=True,
    )
    try:
        query = "取消订单 ORD10003"
        response = await c.chat_service.chat(
            ChatRequest(message=query, user_id="u_demo"), trace_id="original"
        )
        action_id = response.pending_action.action_id
        ctx = ToolContext(user_id="u_demo", session_id=response.session_id, trace_id="original")
        await c.chat_service.executor.confirm(action_id, ctx, c.chat_service.spec)
        budget = RunBudget(time.monotonic() + 1000)
        token = current_budget.set(budget)
        try:
            restored = await c.chat_service.runner.run(
                query, ctx, c.chat_service.spec, run_id=response.run_id, resume=True
            )
            assert "已取消" in restored["answer"]
            assert "pending_action" not in restored
            assert budget.tools == response.diagnostics["tool_calls"]
            assert budget.deadline < time.monotonic() + 1000
        finally:
            current_budget.reset(token)
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(AuditLogRow)) == 1
        with pytest.raises(InvalidRequestError):
            await c.chat_service.runner.run(
                query,
                ctx.model_copy(update={"user_id": "other"}),
                c.chat_service.spec,
                run_id=response.run_id,
                resume=True,
            )
        with pytest.raises(InvalidRequestError, match="input changed"):
            await c.chat_service.runner.run(
                "另一项请求", ctx, c.chat_service.spec, run_id=response.run_id, resume=True
            )
    finally:
        await c.aclose()


def test_consultation_prohibition_survives_supplement_but_explicit_new_authorization_releases_it():
    cases = BusinessCases()
    facts = cases.prepare({}, "ORD10002退货只咨询，不要操作")
    case_id = facts["active_case"]["case_id"]
    facts["active_case"]["stage"] = "needs_input"
    facts = cases.prepare(facts, "尺码不合适")
    assert facts["active_case"]["case_id"] == case_id
    assert facts["active_case"]["proposals_forbidden"]
    facts = cases.prepare(facts, "现在帮我申请退货")
    assert not facts["active_case"]["proposals_forbidden"]


@pytest.mark.asyncio
async def test_cancel_propagates_without_node_retry():
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await asyncio.Event().wait()

    store = SimpleNamespace(cancel_run=AsyncMock())
    node = RuntimeNode("main", operation)
    ctx = ToolContext(user_id="u", session_id="s", trace_id="t")
    task = asyncio.create_task(AgentRuntime().execute(node, context=ctx, run_id="r", store=store))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.cancel_run.assert_awaited_once_with("r", ctx)


@pytest.mark.asyncio
async def test_stale_order_artifact_is_not_returned_as_current_evidence(tmp_path):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/stale.db")
    )
    try:
        ctx = ToolContext(user_id="u", session_id="s", trace_id="t")
        c.chat_service.store.live_ttl = -1
        ref = await c.chat_service.store.put(
            {"success": True, "output": {"status": "paid"}}, ctx, tool_name="get_order"
        )
        result = await c.chat_service.store.read(ref, ctx)
        assert result["success"] is False and result["error_code"] == "STALE_OBSERVATION"
        assert "output" not in result
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_requested_state_does_not_prove_approval():
    from app.harness.validation import AnswerValidator

    validator = AnswerValidator(SimpleNamespace(), semantic_enabled=False)
    value = await validator.validate(
        "退款进度",
        {
            "answer": "审核已通过",
            "status": "completed",
            "observations": [
                {
                    "name": "get_refund_status",
                    "output": {"success": True, "output": {"refunds": [{"status": "requested"}]}},
                }
            ],
            "events": [],
        },
    )
    assert value["status"] == "validation_failed"
    assert value["failure"]["reason"] == "unverified_approval"


@pytest.mark.asyncio
async def test_consultation_without_operation_request_cannot_prepare_return(tmp_path):
    import json

    from app.repositories.models import ActionRow

    def native(name, args, call_id):
        return LLMResponse(
            "",
            tool_calls=[
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
            finish_reason="tool_calls",
        )

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/consult.db"),
        seed=True,
    )
    c.chat_service.runner.llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                native(
                    "create_return_request",
                    {"order_id": "ORD10002", "reason": "裂纹"},
                    "unauthorized-proposal",
                ),
                native(
                    "ask_user",
                    {
                        "question": "请补充裂纹的情况以便核对资格。",
                        "missing_fields": ["quality_details"],
                    },
                    "clarify",
                ),
            ]
        )
    )
    try:
        response = await c.chat_service.chat(
            ChatRequest(message="ORD10002杯子有裂纹，可以退货吗？", user_id="u_demo"),
            trace_id="consult",
        )
        assert response.pending_action is None
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ActionRow)) == 0
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_plain_text_clarification_repairs_into_resumable_case(tmp_path):
    import json

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/clarify.db"),
        seed=True,
    )
    llm = AsyncMock(
        side_effect=[
            LLMResponse("您尚未说明退货原因，请先补充具体原因。"),
            LLMResponse(
                "",
                tool_calls=[
                    {
                        "id": "clarify",
                        "type": "function",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {"question": "请补充退货原因。", "missing_fields": ["reason"]},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                "",
                tool_calls=[
                    {
                        "id": "proposal",
                        "type": "function",
                        "function": {
                            "name": "create_return_request",
                            "arguments": json.dumps(
                                {"order_id": "ORD10002", "reason": "杯子有裂纹"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    c.chat_service.runner.llm = SimpleNamespace(generate=llm)
    try:
        first = await c.chat_service.chat(
            ChatRequest(message="帮我退货，订单ORD10002", user_id="u_demo"), trace_id="clarify-1"
        )
        second = await c.chat_service.chat(
            ChatRequest(message="杯子有裂纹", user_id="u_demo", session_id=first.session_id),
            trace_id="clarify-2",
        )
        assert first.status == "needs_input"
        assert (
            first.diagnostics["business_case"]["case_id"]
            == second.diagnostics["business_case"]["case_id"]
        )
        assert second.pending_action is not None
        assert llm.await_count == 3
    finally:
        await c.aclose()
