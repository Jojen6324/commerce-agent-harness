from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import LLMProviderError
from app.harness.agentic_core import active_run_id
from app.harness.validation import (
    AnswerValidator,
    FaithfulnessVerdict,
)
from app.schemas.retrieval import RetrievedChunk
from app.services.chat_service import ChatService, validation_runtime_context
from app.tools.base import ToolContext


def _chunk():
    return RetrievedChunk(
        chunk_id="return_policy_1",
        document_id="return-policy-v3",
        title="退货政策",
        content="未拆封耳机可以申请退货；激活设备除外。",
        score=1.0,
        policy_version="v3",
        section="耳机",
    )


def _result(answer="未拆封耳机可以申请退货 [return_policy_1]"):
    return {
        "status": "completed",
        "answer": answer,
        "observations": [
            {
                "name": "search_knowledge",
                "arguments": {"query": "耳机退货条件"},
                "call_id": "search1",
                "output": {
                    "success": True,
                    "output": {"chunks": [_chunk().model_dump(mode="json")]},
                },
            }
        ],
        "events": [],
    }


@pytest.mark.asyncio
async def test_answer_validator_uses_structured_faithfulness_verdict_and_attaches_sources():
    verdict = FaithfulnessVerdict(
        verdict="pass",
        evidence_refs=["return_policy_1"],
    )
    verifier = AsyncMock(return_value=verdict)
    validator = AnswerValidator(SimpleNamespace(), verifier=verifier)

    result = await validator.validate("耳机能退吗？", _result())

    assert result["status"] == "completed"
    assert result["validation"]["status"] == "passed"
    verifier.assert_awaited_once()
    assert verifier.await_args.kwargs["known_refs"] >= {"return_policy_1", "search_knowledge"}


@pytest.mark.asyncio
async def test_unknown_faithfulness_reference_fails_closed_and_preserves_repair_guidance():
    verifier = AsyncMock(
        return_value=FaithfulnessVerdict(
            verdict="pass",
            evidence_refs=["made_up_chunk"],
        )
    )
    validator = AnswerValidator(SimpleNamespace(), verifier=verifier)

    result = await validator.validate("耳机能退吗？", _result(), allow_correction=False)

    assert result["status"] == "validation_failed"
    attempt = result["validation"]["attempts"][0]
    assert attempt["verdict"]["verdict"] == "fail"
    assert attempt["unknown_references"] == ["made_up_chunk"]
    assert "未通过审核" in result["answer"] and "未发送" in result["answer"]


@pytest.mark.asyncio
async def test_verifier_unavailable_fails_closed():
    verifier = AsyncMock(side_effect=LLMProviderError("offline", retryable=True))
    validator = AnswerValidator(SimpleNamespace(), verifier=verifier)

    result = await validator.validate("耳机能退吗？", _result(), allow_correction=False)

    assert result["status"] == "failed"
    assert result["validation"]["status"] == "unavailable"
    assert result["failure"]["stage"] == "faithfulness_agent"


@pytest.mark.asyncio
async def test_faithfulness_uses_shared_runner_read_only_tools_and_root_trace_parent():
    child_answer = (
        '{"verdict":"pass","issues":[],"evidence_refs":["search_knowledge"],'
        '"correction_guidance":""}'
    )
    captured = {}

    class Runner:
        async def run(self, query, context, spec, **kwargs):
            captured.update(query=query, context=context, spec=spec, **kwargs)
            return {
                "run_id": "faith_child",
                "status": "completed",
                "answer": child_answer,
                "events": [],
            }

    context = ToolContext(user_id="u1", session_id="s1", trace_id="trace1")
    store = SimpleNamespace(record=AsyncMock())
    service = SimpleNamespace(runner=Runner(), store=store)
    runtime_token = validation_runtime_context.set(
        {"context": context, "facts": {"last_order": {"order_id": "ORD1"}}, "root_run_id": "root1"}
    )
    run_token = active_run_id.set("root1")
    try:
        verdict = await ChatService._verify_faithfulness(
            service,
            query="核实政策",
            answer="已核实 [return_policy_1]",
            observations=_result()["observations"],
            chunks=[_chunk()],
            tool_facts=[{"name": "get_order", "result": {"status": "shipped"}}],
            known_refs={"search_knowledge", "return_policy_1"},
            tool_contracts=[],
        )
    finally:
        active_run_id.reset(run_token)
        validation_runtime_context.reset(runtime_token)

    assert verdict.verdict == "pass"
    assert captured["spec"].name == "critic"
    assert captured["spec"].tools == {"read_artifact"}
    bundle = captured["initial_context"]["critic_input"]
    assert bundle["candidate_answer"] == "已核实 [return_policy_1]"
    assert set(bundle) == {
        "original_user_question",
        "candidate_answer",
        "necessary_conversation_context",
        "evidence_index",
    }
    assert bundle["evidence_index"][0]["refs"] == ["return_policy_1", "search_knowledge"]
    assert not verdict.evidence_checked
    store.record.assert_awaited_once()
    assert store.record.await_args.kwargs["parent_run_id"] == "root1"
