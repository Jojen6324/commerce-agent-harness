"""Critic boundaries use scripted providers; no real model or scoring changes."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError, LLMOutputError, ToolPermissionError
from app.harness.budget import BudgetedProvider, RunBudget, current_budget
from app.harness.executor import ToolExecutor
from app.harness.receipts import support_receipt
from app.harness.runner import AgentRunner, AgentSpec, active_run_id
from app.harness.validation import AnswerValidator, FaithfulnessVerdict
from app.prompts.faithfulness import CRITIC_AGENT_PROMPT
from app.providers.base import LLMResponse
from app.schemas.common import TokenUsage
from app.services.chat_service import ChatService, validation_runtime_context
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry


def verdict(kind="pass", **kwargs):
    return LLMResponse(
        FaithfulnessVerdict(verdict=kind, **kwargs).model_dump_json(),
        usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def read(ref="art_source", identifier="read1"):
    return native("read_artifact", {"ref": ref}, identifier)


def native(name, args, identifier):
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
        usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def context():
    return ToolContext(user_id="u", session_id="s", trace_id="t")


def observation():
    return {
        "name": "get_order",
        "call_id": "observed1",
        "artifact_ref": "art_source",
        "arguments": {"order_id": "ORD1"},
        "output": {
            "success": True,
            "output": {
                "order_id": "ORD1",
                "status": "pending",
                "private_payload": "DO_NOT_PRELOAD" * 4000,
            },
        },
    }


async def review(responses, material=None, *, error=None, budget=None):
    provider = SimpleNamespace(generate=AsyncMock(side_effect=responses))
    store = SimpleNamespace(
        read=AsyncMock(
            return_value=material
            if material is not None
            else {"order_id": "ORD1", "status": "pending"},
            side_effect=error,
        ),
        put=AsyncMock(),
        record=AsyncMock(),
    )
    executor = ToolExecutor(ToolRegistry(), None)
    executor.artifacts = store
    runner = AgentRunner(BudgetedProvider(provider), executor, Settings())
    service = SimpleNamespace(runner=runner, store=store)
    root_token = active_run_id.set("root")
    runtime_token = validation_runtime_context.set(
        {
            "context": context(),
            "root_run_id": "root",
            "facts": {"large_duplicate_facts": "DO_NOT_PRELOAD" * 4000},
            "history": [SimpleNamespace(role="user", content="只查询，不要操作")],
        }
    )
    budget_token = current_budget.set(budget)
    try:
        result = await ChatService._verify_faithfulness(
            service,
            query="它现在是什么状态？",
            answer="订单仍在申请中，不代表审批或执行完成。",
            observations=[observation()],
            chunks=[],
            tool_facts=[{"result": {"private": "DO_NOT_PRELOAD"}}],
            known_refs={"get_order", "observed1"},
            tool_contracts=[{"huge": "DO_NOT_PRELOAD"}],
        )
        return result, provider, store
    finally:
        current_budget.reset(budget_token)
        validation_runtime_context.reset(runtime_token)
        active_run_id.reset(root_token)


@pytest.mark.asyncio
async def test_default_input_is_only_question_background_candidate_and_real_index():
    result, provider, store = await review([verdict()])
    messages = provider.generate.await_args.args[0]
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "DO_NOT_PRELOAD" not in serialized
    assert "只查询，不要操作" in serialized
    assert "art_source" in serialized and "get_order" in serialized
    assert "订单仍在申请中，不代表审批或执行完成。" in serialized
    assert "不能凭常识判断政策或订单状态错误" in serialized
    assert "必须先读取全部证据" not in serialized
    assert result.verdict == "pass" and not result.evidence_checked
    assert result.evidence_refs == []
    store.read.assert_not_awaited()
    store.put.assert_not_awaited()
    assert provider.generate.await_count == 1


@pytest.mark.asyncio
async def test_single_read_is_full_then_tools_close_and_all_usage_is_shared():
    material = {"order_id": "ORD1", "status": "pending", "detail": "原始材料" * 1500}
    budget = RunBudget(time.monotonic() + 60, max_models=4, max_tools=2, max_tokens=128000)
    result, provider, store = await review(
        [
            read(),
            verdict(
                "revise",
                issues=["候选中的已完成与申请中矛盾"],
                correction_guidance="保留申请中，删除已完成",
                evidence_refs=["get_order"],
            ),
        ],
        material,
        budget=budget,
    )
    assert result.verdict == "revise" and result.evidence_checked
    assert budget.models == 2 and budget.tools == 1 and budget.usage.total_tokens == 240
    store.read.assert_awaited_once_with("art_source", context())
    second = provider.generate.await_args_list[1]
    assert second.kwargs["tools"] == []
    message = next(m for m in second.args[0] if m["role"] == "tool")
    assert json.loads(message["content"]) == material
    assert any("工具现已关闭" in m["content"] for m in second.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "material",
    [
        {"summary": "已完成", "facts_complete": False},
        {"chunks": [{"chunk_id": "c1", "title": "仅标题"}]},
        {"facts": {"status": "pending"}, "evidence_complete": False},
        {"status": "complete", "artifact_ref": "art_source", "summary": "摘要"},
        {"output": {"status": "pending", "truncated": True}},
        {"output": {"content_excerpt": "节选"}},
        {"output": []},
        {"chunks": []},
        ["非结构化材料"],
    ],
)
async def test_incomplete_material_cannot_be_called_verified_even_if_model_passes(material):
    result, _, store = await review([read(), verdict(evidence_checked=True)], material)
    assert result.verdict == "insufficient_evidence"
    assert not result.evidence_checked and not result.evidence_refs
    store.read.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_read_cannot_be_called_verified_even_if_model_passes():
    result, _, _ = await review(
        [read(), verdict(evidence_checked=True)], error=InvalidRequestError("artifact not found")
    )
    assert result.verdict == "insufficient_evidence" and not result.evidence_checked


@pytest.mark.asyncio
async def test_second_read_request_is_not_executed_and_gets_one_output_retry():
    result, provider, store = await review(
        [
            read(),
            read(identifier="read2"),
            verdict("insufficient_evidence", issues=["无法核实疑点"]),
        ]
    )
    store.read.assert_awaited_once()
    assert provider.generate.await_count == 3
    assert result.verdict == "insufficient_evidence"
    assert provider.generate.await_args_list[2].kwargs["tools"] == []


@pytest.mark.asyncio
async def test_schema_retry_is_bounded_and_never_reopens_tools():
    result, provider, store = await review([LLMResponse("普通解释"), verdict()])
    assert result.verdict == "pass" and provider.generate.await_count == 2
    assert provider.generate.await_args_list[1].kwargs["tools"] == []
    store.read.assert_not_awaited()
    with pytest.raises(LLMOutputError, match="JSON|object|invalid|schema"):
        await review([LLMResponse("不是JSON"), LLMResponse("仍不是JSON")])


@pytest.mark.asyncio
async def test_critic_cannot_read_unindexed_artifact_or_query_business_tools():
    executor = ToolExecutor(ToolRegistry(), None)
    executor.artifacts = SimpleNamespace(read=AsyncMock())
    spec = AgentSpec("critic", frozenset({"read_artifact"}), artifact_refs=frozenset({"allowed"}))
    for tool, args in [
        ("read_artifact", {"ref": "other"}),
        ("get_order", {"order_id": "ORD1"}),
        ("delegate_task", {}),
        ("cancel_order", {}),
    ]:
        with pytest.raises(ToolPermissionError):
            await executor.execute(tool, args, context(), spec)
    executor.artifacts.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_unread_pass_has_no_citation_requirement_or_fact_verified_claim():
    verifier = AsyncMock(return_value=FaithfulnessVerdict(verdict="pass"))
    result = await AnswerValidator(SimpleNamespace(), verifier=verifier).validate(
        "订单状态？",
        {"status": "completed", "answer": "订单仍在申请中。", "observations": [observation()]},
    )
    assert result["status"] == "completed"
    assert result["validation"]["review_type"] == "critic"
    assert not result["validation"]["facts_verified"]
    assert not result["validation"]["evidence_checked"]
    verifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_simple_answer_is_reviewed_even_without_business_observations():
    verifier = AsyncMock(return_value=FaithfulnessVerdict(verdict="pass"))
    result = await AnswerValidator(SimpleNamespace(), verifier=verifier).validate(
        "你好，你是？",
        {"status": "completed", "answer": "我是智能客服。", "observations": []},
    )
    verifier.assert_awaited_once()
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_server_rendered_handoff_receipt_still_runs_critic():
    ticket = {
        "ticket_id": "T-1",
        "status": "pending",
        "queue": "human",
        "delivery": "database",
        "connected": False,
    }
    verifier = AsyncMock(return_value=FaithfulnessVerdict(verdict="pass"))
    result = await AnswerValidator(SimpleNamespace(), verifier=verifier).validate(
        "请帮我转人工",
        {
            "status": "completed",
            "answer": support_receipt(ticket),
            "observations": [
                {"name": "escalate_to_human", "output": {"success": True, "output": ticket}}
            ],
        },
    )
    verifier.assert_awaited_once()
    assert result["status"] == "completed"
    assert result["validation"]["review_type"] == "critic"


@pytest.mark.asyncio
async def test_saved_handoff_receipt_uses_the_normal_main_delivery_gate():
    provider = SimpleNamespace(
        generate=AsyncMock(return_value=native("escalate_to_human", {"reason": "人工服务"}, "handoff"))
    )
    receipt = {
        "ticket_id": "T-1",
        "status": "pending",
        "queue": "human",
        "delivery": "database",
        "connected": False,
    }
    executor = SimpleNamespace(
        artifacts=None,
        schemas=lambda _: [{"type": "function", "function": {"name": "escalate_to_human"}}],
        execute=AsyncMock(return_value={"success": True, "output": receipt}),
    )
    runner = AgentRunner(provider, executor, Settings())
    runner.output_validator = AsyncMock(
        side_effect=lambda query, candidate, **kwargs: {
            **candidate,
            "validation": {"status": "passed", "attempts": []},
        }
    )
    result = await runner.run(
        "请帮我转人工", context(), AgentSpec("main", frozenset({"escalate_to_human"}))
    )
    assert result["answer"] == support_receipt(receipt)
    runner.output_validator.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_executor_blocks_unrequested_human_ticket_before_dispatch():
    tool = SimpleNamespace(
        name="escalate_to_human",
        risk=ToolRisk.EXTERNAL,
        parse=lambda args: SimpleNamespace(model_dump=lambda: args),
        run=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda mode: {"success": True})
        ),
    )
    executor = ToolExecutor(SimpleNamespace(get=lambda _: tool), None)
    spec = AgentSpec("main", frozenset({"escalate_to_human"}))
    with pytest.raises(InvalidRequestError, match="尚未明确要求转人工"):
        await executor.execute(
            "escalate_to_human",
            {"reason": "缺少税号"},
            context().model_copy(update={"user_messages": ["ORD10002，不过我没提供税号"]}),
            spec,
            transaction=object(),
        )
    tool.run.assert_not_awaited()
    await executor.execute(
        "escalate_to_human",
        {"reason": "用户要求人工"},
        context().model_copy(update={"user_messages": ["请帮我转人工"]}),
        spec,
        transaction=object(),
    )
    tool.run.assert_awaited_once()


def rejected(candidate, issue="遗漏第二问"):
    return {
        **candidate,
        "answer": "候选未发送",
        "status": "validation_failed",
        "failure": {
            "stage": "output_validation",
            "reason": "revise",
            "issues": [issue],
            "correction_guidance": "补答第二问，保留限制",
        },
        "validation": {
            "status": "revise",
            "attempts": [
                {
                    "stage": "critic",
                    "verdict": {
                        "verdict": "revise",
                        "issues": [issue],
                        "correction_guidance": "补答第二问，保留限制",
                    },
                    "trace_only": "PRIVATE_AUDIT",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_only_current_feedback_once_correction_once_recheck_then_stop():
    provider = SimpleNamespace(
        generate=AsyncMock(side_effect=[LLMResponse("候选一"), LLMResponse("候选二")])
    )
    runner = AgentRunner(provider, SimpleNamespace(schemas=lambda _: []), Settings())
    runner.output_validator = AsyncMock(side_effect=lambda q, c, **kw: rejected(c))
    result = await runner.run(
        "回答两个问题", context(), AgentSpec("main", frozenset(), max_steps=8)
    )
    assert result["status"] == "validation_failed"
    assert runner.output_validator.await_count == 2 and provider.generate.await_count == 2
    assert sum(e["event"] == "output_repair" for e in result["events"]) == 1
    assert len(result["validation"]["attempts"]) == 2
    second_prompt = json.dumps(provider.generate.await_args_list[1].args[0], ensure_ascii=False)
    assert "遗漏第二问" in second_prompt and "PRIVATE_AUDIT" not in second_prompt
    assert '"attempts"' not in second_prompt
    assert result["failure"]["convergence_reason"] == "correction_limit"


@pytest.mark.asyncio
async def test_unchanged_answer_and_evidence_reuse_verdict_without_second_review():
    provider = SimpleNamespace(
        generate=AsyncMock(side_effect=[LLMResponse("同一答复"), LLMResponse("同一答复")])
    )
    runner = AgentRunner(provider, SimpleNamespace(schemas=lambda _: []), Settings())
    runner.output_validator = AsyncMock(side_effect=lambda q, c, **kw: rejected(c))
    result = await runner.run("答两个问题", context(), AgentSpec("main", frozenset()))
    assert result["status"] == "validation_failed" and runner.output_validator.await_count == 1
    assert any(e["event"] == "output_validation_reused" for e in result["events"])
    assert len(result["validation"]["attempts"]) == 1


@pytest.mark.asyncio
async def test_no_remaining_main_step_converges_without_extra_model_call():
    provider = SimpleNamespace(generate=AsyncMock(return_value=LLMResponse("候选答复")))
    runner = AgentRunner(provider, SimpleNamespace(schemas=lambda _: []), Settings())
    runner.output_validator = AsyncMock(side_effect=lambda q, c, **kw: rejected(c))
    result = await runner.run("问题", context(), AgentSpec("main", frozenset(), max_steps=1))
    assert result["status"] == "validation_failed"
    assert provider.generate.await_count == 1
    assert result["failure"]["convergence_reason"] == "main_steps_insufficient"


@pytest.mark.asyncio
async def test_main_repair_uses_executor_role_permissions_to_prevent_repeated_writes():
    registry = ToolRegistry()
    # Role denial occurs before the tool schema/parser or database are accessed.
    registry.register(SimpleNamespace(name="cancel_order", risk=ToolRisk.DESTRUCTIVE))
    executor = ToolExecutor(registry, None)
    executor.schemas = lambda spec: []
    provider = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse("候选答复"),
                native("cancel_order", {"order_id": "ORD1"}, "bad_write"),
            ]
        )
    )
    runner = AgentRunner(provider, executor, Settings())
    runner.output_validator = AsyncMock(side_effect=lambda q, c, **kw: rejected(c))
    result = await runner.run("只查状态", context(), AgentSpec("main", frozenset({"cancel_order"})))
    assert result["status"] == "denied"
    assert provider.generate.await_count == 2 and runner.output_validator.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,output,reason",
    [
        ("退款已到账", {"status": "pending"}, "unverified_refund_completion"),
        ("已提交", {"pending_action": {"action_id": "a1"}}, "unknown_citation_or_action_claim"),
        ("完成了 [invented]", {}, "unknown_citation_or_action_claim"),
    ],
)
async def test_critic_pass_cannot_override_deterministic_states_and_pending_boundary(
    answer, output, reason
):
    verifier = AsyncMock(return_value=FaithfulnessVerdict(verdict="pass"))
    item = observation()
    item["output"] = {"success": True, **output}
    result = await AnswerValidator(SimpleNamespace(), verifier=verifier).validate(
        "查进度",
        {"status": "completed", "answer": answer, "observations": [item]},
    )
    assert result["status"] == "validation_failed" and result["failure"]["reason"] == reason
    verifier.assert_not_awaited()


def test_critic_prompt_separates_application_approval_execution_and_settlement():
    assert "申请、待确认、审批、执行和到账" in CRITIC_AGENT_PROMPT
    assert "默认没有查看业务依据" in CRITIC_AGENT_PROMPT
    assert "不做措辞润色" in CRITIC_AGENT_PROMPT
    assert "必须把用户明确提出的每个问题拆成可回答的事实或请求" in CRITIC_AGENT_PROMPT
    assert "只说“运输中”并解释丢件政策仍属遗漏" in CRITIC_AGENT_PROMPT
    assert "用户明确授权一致" in CRITIC_AGENT_PROMPT


@pytest.mark.asyncio
async def test_parallel_artifact_requests_still_dispatch_only_one_read():
    calls = read().tool_calls + read(identifier="second").tool_calls
    result, _, store = await review(
        [LLMResponse("", tool_calls=calls, finish_reason="tool_calls"), verdict()]
    )
    store.read.assert_awaited_once()
    assert result.verdict == "insufficient_evidence"


@pytest.mark.asyncio
async def test_changed_evidence_allows_recheck_but_duplicate_snapshot_does_not():
    for changed in (False, True):
        original = observation()
        original["output"] = {"success": True, "status": "pending"}
        provider = SimpleNamespace(
            generate=AsyncMock(
                side_effect=[
                    LLMResponse("相同候选"),
                    native("get_order", {"order_id": "ORD1"}, "new_call"),
                    LLMResponse("相同候选"),
                ]
            )
        )
        registry = ToolRegistry()
        registry.register(SimpleNamespace(name="get_order", risk=ToolRisk.READ))
        executor = SimpleNamespace(
            registry=registry,
            schemas=lambda _: [],
            execute=AsyncMock(
                return_value={
                    "success": True,
                    "status": "approved" if changed else "pending",
                }
            ),
        )
        runner = AgentRunner(provider, executor, Settings())
        runner.output_validator = AsyncMock(side_effect=lambda q, c, **kw: rejected(c))
        result = await runner.run(
            "查询",
            context(),
            AgentSpec("main", frozenset({"get_order"})),
            initial_observations=[original],
        )
        assert result["status"] == "validation_failed"
        assert runner.output_validator.await_count == (2 if changed else 1)


@pytest.mark.asyncio
async def test_critic_artifact_read_retains_runtime_owner_and_session_isolation(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.repositories.db import create_engine, init_schema
    from app.repositories.runtime import RuntimeStore

    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/critic.db"))
    await init_schema(engine)
    try:
        store = RuntimeStore(async_sessionmaker(engine, expire_on_commit=False))
        ref = await store.put({"order_id": "ORD1", "status": "pending"}, context())
        executor = ToolExecutor(ToolRegistry(), None)
        executor.artifacts = store
        spec = AgentSpec("critic", frozenset({"read_artifact"}), artifact_refs=frozenset({ref}))
        for ctx in (
            context().model_copy(update={"user_id": "other"}),
            context().model_copy(update={"session_id": "other"}),
        ):
            with pytest.raises(InvalidRequestError, match="artifact not found"):
                await executor.execute("read_artifact", {"ref": ref}, ctx, spec)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_last_main_repair_step_closes_lookups_but_can_deliver_answer():
    captured_specs = []

    def schemas(spec):
        captured_specs.append(spec)
        return []

    registry = ToolRegistry()
    registry.register(SimpleNamespace(name="get_order", risk=ToolRisk.READ))
    provider = SimpleNamespace(
        generate=AsyncMock(side_effect=[LLMResponse("候选一"), LLMResponse("修正版")])
    )
    runner = AgentRunner(provider, SimpleNamespace(registry=registry, schemas=schemas), Settings())
    runner.output_validator = AsyncMock(
        side_effect=[
            rejected({"answer": "候选一"}),
            {
                "answer": "修正版",
                "status": "completed",
                "validation": {"status": "passed", "attempts": []},
            },
        ]
    )
    result = await runner.run(
        "查询", context(), AgentSpec("main", frozenset({"get_order", "ask_user"}), max_steps=2)
    )
    assert result["status"] == "completed"
    assert captured_specs[-1].tools == {"ask_user"}
    assert any(e["event"] == "output_repair_finalizing" for e in result["events"])
    assert provider.generate.await_count == 2


@pytest.mark.asyncio
async def test_unread_model_claim_of_evidence_check_is_not_trusted():
    result, _, store = await review([verdict(evidence_checked=True, evidence_refs=["get_order"])])
    assert result.verdict == "pass"
    assert not result.evidence_checked and result.evidence_refs == []
    store.read.assert_not_awaited()
