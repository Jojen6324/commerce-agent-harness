import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.agents.nodes.faithfulness import faithfulness_node
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy, ToolRisk
from app.core.exceptions import LLMProviderError
from app.guardrails.hallucination import FaithfulnessResult
from app.harness.budget import RunBudget, current_budget
from app.harness.completion import policy_evidence_issue
from app.harness.executor import ToolExecutor
from app.harness.runner import AgentRunner, AgentSpec
from app.harness.validation import AnswerValidator
from app.providers.base import LLMResponse
from app.repositories.models import ActionRow, OrderRow, RunRow
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import RetrievedChunk
from app.services.container import build_container
from app.tools.base import ToolContext


def native(name, args, identifier="c1"):
    return LLMResponse(
        "",
        tool_calls=[
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verbal",
    [
        LLMResponse("确认后我就提交取消。请问是否继续？"),
        native(
            "ask_user",
            {"question": "是否确认提交取消申请？", "missing_fields": ["确认提交取消申请"]},
        ),
    ],
)
async def test_verbal_approval_repaired_into_persisted_action_and_replayed_once(tmp_path, verbal):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/repair.db"),
        seed=True,
    )
    try:
        model = SimpleNamespace(
            generate=AsyncMock(
                side_effect=[verbal, native("cancel_order", {"order_id": "ORD10003"}, "c2")]
            )
        )
        c.chat_service.runner.llm = model
        proposal = await c.chat_service.chat(
            ChatRequest(message="取消ORD10003", user_id="u_demo"), trace_id="t"
        )
        assert proposal.status == "awaiting_confirmation" and proposal.pending_action
        assert any(e["event"] == "completion_repair" for e in proposal.events)
        async with c.session_factory() as db:
            assert (await db.get(OrderRow, "ORD10003")).status == "paid"
            assert await db.scalar(select(func.count()).select_from(ActionRow)) == 1
        context = ToolContext(user_id="u_demo", session_id=proposal.session_id, trace_id="t")
        from app.core.exceptions import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            await c.chat_service.executor.confirm(
                proposal.pending_action.action_id,
                context.model_copy(update={"user_id": "other"}),
                c.chat_service.spec,
            )
        request = ChatRequest(
            message="确认",
            user_id="u_demo",
            session_id=proposal.session_id,
            confirm_action_id=proposal.pending_action.action_id,
        )
        first = await c.chat_service.chat(request, trace_id="t")
        replay = await c.chat_service.chat(request, trace_id="t")
        assert first.status == "completed" and replay.answer == first.answer
        async with c.session_factory() as db:
            assert (await db.get(OrderRow, "ORD10003")).status == "cancelled"
        assert model.generate.await_count == 2
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_repeated_unfinished_promise_fails_after_one_repair():
    model = SimpleNamespace(generate=AsyncMock(return_value=LLMResponse("我查一下相关政策条款。")))
    executor = SimpleNamespace(schemas=lambda _: [], execute=AsyncMock())
    result = await AgentRunner(model, executor, Settings()).run(
        "退货政策",
        ToolContext(user_id="u", session_id="s", trace_id="t"),
        AgentSpec("main", frozenset()),
    )
    assert result["status"] == "validation_failed" and model.generate.await_count == 2
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_facts_not_replaced_by_spurious_repair():
    model = SimpleNamespace(
        generate=AsyncMock(return_value=LLMResponse("订单还没发货。如需政策，我可以帮您查一下。"))
    )
    executor = SimpleNamespace(schemas=lambda _: [], execute=AsyncMock())
    result = await AgentRunner(model, executor, Settings()).run(
        "订单状态",
        ToolContext(user_id="u", session_id="s", trace_id="t"),
        AgentSpec("main", frozenset()),
    )
    assert result["status"] == "completed" and model.generate.await_count == 1


@pytest.mark.asyncio
async def test_knowledge_has_separate_timeout_and_obeys_parent_deadline():
    async def run(*args):
        await asyncio.sleep(0.04)
        return SimpleNamespace(model_dump=lambda **_: {"success": True})

    tool = SimpleNamespace(
        risk=ToolRisk.READ, parse=lambda _: SimpleNamespace(model_dump=lambda: {}), run=run
    )
    registry = SimpleNamespace(get=lambda _: tool)
    executor = ToolExecutor(registry, None, timeout=0.005, knowledge_timeout=0.2)
    spec = AgentSpec("main", frozenset(["answer_question", "get_product"]))
    context = ToolContext(user_id="u", session_id="s", trace_id="t")
    assert (await executor.execute("answer_question", {}, context, spec, transaction=object()))[
        "success"
    ]
    with pytest.raises(TimeoutError):
        await executor.execute("get_product", {}, context, spec, transaction=object())
    token = current_budget.set(RunBudget(time.monotonic() + 0.005))
    try:
        with pytest.raises(TimeoutError):
            await executor.execute("answer_question", {}, context, spec, transaction=object())
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_outer_deadline_is_not_reported_as_completed_and_is_persisted(tmp_path):
    c = await build_container(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/deadline.db",
            agent_deadline_seconds=0.01,
        )
    )

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    c.chat_service.runner.llm = SimpleNamespace(generate=slow)
    try:
        response = await c.chat_service.chat(ChatRequest(message="你好", user_id="u"), trace_id="t")
        assert (
            response.status == "budget_exhausted" and response.error_code == "RUN_BUDGET_EXHAUSTED"
        )
        assert response.run_id and response.diagnostics["failure"]["reason"] == "deadline"
        async with c.session_factory() as db:
            assert (await db.get(RunRow, response.run_id)).status == "budget_exhausted"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_provider_failure_keeps_code_retryability_and_event(tmp_path):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/provider.db")
    )
    c.chat_service.runner.llm = SimpleNamespace(
        generate=AsyncMock(side_effect=LLMProviderError("AGNES HTTP 429", rate_limited=True))
    )
    try:
        response = await c.chat_service.chat(ChatRequest(message="你好", user_id="u"), trace_id="t")
        assert response.status == "failed" and response.error_code == "LLM_RATE_LIMITED"
        assert response.diagnostics["failure"]["retryable"]
        assert any(e.get("reason") == "AGNES HTTP 429" for e in response.events)
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_checker_fallback_reports_unavailability_without_claiming_missing_model():
    chunk = RetrievedChunk(
        document_id="policy", chunk_id="policy_1", title="退货", content="七日内可申请", score=1
    )
    checker = SimpleNamespace(
        check=AsyncMock(
            return_value=FaithfulnessResult(
                supported=False,
                claims_grounded=False,
                answers_question=False,
                scope_preserved=False,
                critical_conditions_complete=False,
                rationale="checker_unavailable",
            )
        )
    )
    result = await faithfulness_node(
        {
            "user_query": "退货",
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": [chunk],
            "final_answer": "可退",
        },
        checker,
        SimpleNamespace(),
    )
    assert "未配置" not in result["final_answer"] and result["extra"]["degraded"]
    assert result["citations"][0].chunk_id == "policy_1"
    model = SimpleNamespace(generate=AsyncMock(side_effect=LLMProviderError("unavailable")))
    result = await AnswerValidator(model).validate(
        "订单",
        {
            "status": "completed",
            "answer": "订单已发货",
            "observations": [{"name": "get_order", "output": {"output": {"status": "shipped"}}}],
        },
    )
    assert result["status"] == "failed" and result["failure"]["code"] == "LLM_PROVIDER_UNAVAILABLE"
    assert result["failure"]["stage"] == "faithfulness"


@pytest.mark.asyncio
async def test_policy_answer_with_missing_fields_is_still_faithfulness_checked():
    chunk = RetrievedChunk(
        document_id="return_policy",
        chunk_id="return_policy_15d",
        title="退货政策",
        content="质量问题需在签收后15日内提交凭证。",
        score=1,
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                '{"supported":true,"claims_grounded":true,"answers_question":true,"scope_preserved":true,'
                '"critical_conditions_complete":true,"hallucinated_spans":[],'
                '"missing_conditions":[],"rationale":"与政策一致"}'
            )
        )
    )
    result = {
        "status": "needs_input",
        "answer": "质量问题需在15日内提交凭证 [return_policy_15d]\n\n还需要补充：请提供订单号。",
        "observations": [
            {
                "name": "search_knowledge",
                "output": {"success": True, "output": {"chunks": [chunk.model_dump(mode="json")]}},
            },
            {
                "name": "ask_user",
                "output": {
                    "success": True,
                    "output": {"status": "needs_input", "missing_fields": ["order_id"]},
                },
            },
        ],
    }

    validated = await AnswerValidator(llm).validate("破损怎么退", result)

    assert validated["status"] == "needs_input"
    assert llm.generate.await_count == 1


@pytest.mark.asyncio
async def test_policy_scope_failure_is_repaired_with_the_missing_condition():
    digital = RetrievedChunk(
        document_id="return_electronics",
        chunk_id="digital_15d",
        title="数码退货细则",
        section="时效",
        content="数码商品质量问题超过15天转保修通道，不能原单退货。",
        score=1,
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    '{"supported":true,"claims_grounded":false,"answers_question":true,"scope_preserved":false,'
                    '"critical_conditions_complete":false,"hallucinated_spans":["所有商品"],'
                    '"missing_conditions":["超过15天规则仅适用于数码商品"],'
                    '"rationale":"把数码规则泛化到所有商品"}'
                ),
                LLMResponse(
                    '{"answer":"资料只明确数码商品超过15天转保修；其他品类需核实。'
                    '请提供商品或订单信息。[digital_15d]",'
                    '"citation_chunk_ids":["digital_15d"]}'
                ),
                LLMResponse(
                    '{"supported":true,"claims_grounded":true,"answers_question":true,"scope_preserved":true,'
                    '"critical_conditions_complete":true,"hallucinated_spans":[],'
                    '"missing_conditions":[],"rationale":"已限定品类并说明未知范围"}'
                ),
            ]
        )
    )
    result = await AnswerValidator(llm).validate(
        "超过15天的质量问题还能退吗",
        {
            "status": "completed",
            "answer": "超过15天的质量问题都不能原单退货 [digital_15d]",
            "observations": [
                {
                    "name": "search_knowledge",
                    "output": {
                        "success": True,
                        "output": {"chunks": [digital.model_dump(mode="json")]},
                    },
                }
            ],
        },
    )
    assert result["status"] == "completed"
    assert "只明确数码商品" in result["answer"]
    assert "[digital_15d]" in result["answer"]
    rewrite_prompt = llm.generate.await_args_list[1].args[0][1]["content"]
    assert "超过15天规则仅适用于数码商品" in rewrite_prompt


@pytest.mark.asyncio
async def test_correcting_partial_answer_preserves_need_for_user_input():
    chunk = RetrievedChunk(
        document_id="policy",
        chunk_id="p1",
        title="政策",
        content="凭证窗口为12日",
        score=1,
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    '{"supported":false,"claims_grounded":false,"answers_question":true,"scope_preserved":true,'
                    '"critical_conditions_complete":false,"hallucinated_spans":["15日"],'
                    '"missing_conditions":["期限为12日"],"rationale":"期限错误"}'
                ),
                LLMResponse(
                    '{"answer":"应在12日内提交凭证。请提供订单号。","citation_chunk_ids":["p1"]}'
                ),
                LLMResponse(
                    '{"supported":true,"claims_grounded":true,"answers_question":true,"scope_preserved":true,'
                    '"critical_conditions_complete":true,"hallucinated_spans":[],'
                    '"missing_conditions":[],"rationale":"符合原文"}'
                ),
            ]
        )
    )
    result = await AnswerValidator(llm).validate(
        "如何办理",
        {
            "status": "needs_input",
            "answer": "15日内提交。请提供订单号。",
            "observations": [
                {
                    "name": "search_knowledge",
                    "output": {
                        "success": True,
                        "output": {"chunks": [chunk.model_dump(mode="json")]},
                    },
                }
            ],
        },
    )
    assert result["status"] == "needs_input"
    assert "12日" in result["answer"] and "订单号" in result["answer"]


def test_policy_evidence_gate_rejects_successful_empty_search():
    observations = [
        {"name": "search_knowledge", "output": {"success": True, "output": {"chunks": []}}}
    ]
    assert policy_evidence_issue("破损怎么退", observations) == "policy_without_evidence"
    observations[0]["output"]["output"]["chunks"] = [
        {"chunk_id": "return_01", "content": "签收后15日内提交凭证"}
    ]
    assert policy_evidence_issue("破损怎么退", observations) is None


def test_policy_evidence_accepts_specialist_sources_without_fixed_tool_path():
    observation = {
        "name": "delegate_task",
        "output": {
            "status": "completed",
            "evidence_refs": ["return_01"],
            "chunks": [{"chunk_id": "return_01", "content": "提交凭证"}],
        },
    }
    assert policy_evidence_issue("破损怎么退", [observation]) is None
    observation["output"]["chunks"] = []
    assert policy_evidence_issue("破损怎么退", [observation]) == "policy_without_evidence"


@pytest.mark.asyncio
async def test_catalog_search_accepts_combined_terms_without_broadening_literal_wildcards(tmp_path):
    from app.repositories.models import ProductRow
    from app.repositories.order_repository import OrderRepository

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/products.db"),
        seed=True,
    )
    try:
        async with c.session_factory() as db:
            repo = OrderRepository(db)
            assert [p.sku for p in await repo.search_products("T恤/纯棉")] == ["SKU-TEE"]
            assert not await repo.search_products("耳机/纯棉")
            assert not await repo.search_products("T恤/%")
            product = await db.get(ProductRow, "SKU-TEE")
            product.name = "100%纯棉T恤"
            await db.commit()
            assert [p.sku for p in await repo.search_products("100%")] == ["SKU-TEE"]
    finally:
        await c.aclose()


def test_extractive_fallback_keeps_one_source_and_its_complete_reference():
    from app.agents.nodes.generate import _extractive_fallback

    answer = _extractive_fallback(
        "退货",
        "[first_1] 政策一\n定制不适用无理由。\n\n[second_2] 政策二\n无关的其他规则。",
        "(无)",
    )
    assert "定制不适用无理由。" in answer and "[first_1]" in answer
    assert "second_2" not in answer and "无关" not in answer


@pytest.mark.asyncio
async def test_missing_or_invented_refund_reason_returns_to_clarification_without_proposal(
    tmp_path,
):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/reason.db"),
        seed=True,
    )
    try:
        c.chat_service.runner.llm = SimpleNamespace(
            generate=AsyncMock(
                side_effect=[
                    native(
                        "create_refund_request", {"order_id": "ORD10002", "reason": "用户要求退款"}
                    ),
                    native(
                        "create_refund_request",
                        {"order_id": "ORD10002", "reason": "商品质量问题"},
                        "invented",
                    ),
                    native(
                        "ask_user",
                        {"question": "请提供具体退款原因。", "missing_fields": ["reason"]},
                        "ask",
                    ),
                ]
            )
        )
        response = await c.chat_service.chat(
            ChatRequest(message="我要退ORD10002的钱", user_id="u_demo"), trace_id="t"
        )
        assert response.status == "needs_input" and response.pending_action is None
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ActionRow)) == 0
            assert (await db.get(OrderRow, "ORD10002")).status == "delivered"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_empty_final_answer_gets_one_repair_without_forcing_tools():
    model = SimpleNamespace(
        generate=AsyncMock(side_effect=[LLMResponse("..."), LLMResponse("你好")])
    )
    executor = SimpleNamespace(schemas=lambda _: [], execute=AsyncMock())
    result = await AgentRunner(model, executor, Settings()).run(
        "你好",
        ToolContext(user_id="u", session_id="s", trace_id="t"),
        AgentSpec("main", frozenset()),
    )
    assert result["status"] == "completed" and result["answer"] == "你好"
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_promise_requires_receipt_and_accepts_successful_queue_result():
    model = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("我会为您转接人工审核。请提供订单号。"),
                native("escalate_to_human", {"reason": "用户要求人工审核退款"}),
                LLMResponse("已为您转接人工队列，工单T-1。"),
            ]
        )
    )
    executor = SimpleNamespace(
        schemas=lambda _: [],
        execute=AsyncMock(
            return_value={"success": True, "output": {"queue": "human", "ticket_id": "T-1"}}
        ),
    )
    result = await AgentRunner(model, executor, Settings()).run(
        "人工审核退款",
        ToolContext(user_id="u", session_id="s", trace_id="t"),
        AgentSpec("main", frozenset(["escalate_to_human"])),
    )
    assert result["status"] == "completed" and model.generate.await_count == 3
    executor.execute.assert_awaited_once()
