import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.nodes.faithfulness import self_correct_answer
from app.core.exceptions import LLMProviderError
from app.guardrails.hallucination import FaithfulnessResult
from app.harness.completion import policy_evidence_issue
from app.harness.validation import AnswerValidator
from app.providers.base import LLMResponse
from app.schemas.retrieval import RetrievedChunk


def verdict(supported=True, **kwargs):
    return FaithfulnessResult(
        supported=supported,
        claims_grounded=supported,
        answers_question=True,
        scope_preserved=supported,
        critical_conditions_complete=supported,
        **kwargs,
    )


def chunk():
    return RetrievedChunk(
        chunk_id="digital",
        document_id="electronics",
        title="数码退货细则",
        content="数码商品签收超过15天转保修，不能原单退货。",
        score=1,
    )


@pytest.mark.asyncio
async def test_truncated_rewrite_recovers_without_replacing_answer_with_first_search_hit():
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMProviderError("rewrite truncated", retryable=False, finish_reason="length"),
                LLMResponse(
                    json.dumps(
                        {
                            "answer": "仅数码商品超过15天转保修，其他品类需核实。",
                            "citation_chunk_ids": ["digital"],
                        }
                    )
                ),
            ]
        )
    )
    checker = SimpleNamespace(check=AsyncMock(return_value=verdict()))
    audit = []
    answer, judged, _ = await self_correct_answer(
        llm=llm,
        checker=checker,
        query="超15天还能退吗",
        answer="所有商品都不能退",
        chunks=[chunk()],
        tool_results_text="",
        first_result=verdict(False, missing_conditions=["限定数码范围"]),
        audit=audit,
    )
    assert judged.supported and "其他品类需核实" in answer and "[digital]" in answer
    assert checker.check.await_count == 1
    assert "rewrite truncated" in llm.generate.await_args_list[1].args[0][1]["content"]
    assert audit[0]["error"]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_semantic_retry_uses_latest_verdict_and_keeps_business_tool_references():
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse('{"answer":"一律不能退 [digital]","citation_chunk_ids":[]}'),
                LLMResponse(
                    '{"answer":"数码超15天转保修；请提供订单号核实 [get_order] [digital]","citation_chunk_ids":["digital"]}'
                ),
            ]
        )
    )
    checker = SimpleNamespace(
        check=AsyncMock(
            side_effect=[
                verdict(False, missing_conditions=["不能把数码限制推广到所有品类"]),
                verdict(),
            ]
        )
    )
    answer, judged, _ = await self_correct_answer(
        llm=llm,
        checker=checker,
        query="超15天还能退吗",
        answer="都不能退",
        chunks=[chunk()],
        tool_results_text="已查询get_order",
        first_result=verdict(False, rationale="未保留范围"),
        allowed_refs={"get_order"},
    )
    assert judged.supported and "[get_order]" in answer
    assert "不能把数码限制推广到所有品类" in llm.generate.await_args_list[1].args[0][1]["content"]


@pytest.mark.asyncio
async def test_unknown_inline_reference_cannot_bypass_rewrite_reference_validation():
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse('{"answer":"可退 [invented_source]","citation_chunk_ids":[]}')
        )
    )
    checker = SimpleNamespace(check=AsyncMock(return_value=verdict()))
    _, judged, _ = await self_correct_answer(
        llm=llm,
        checker=checker,
        query="能退吗",
        answer="原答案",
        chunks=[chunk()],
        tool_results_text="",
        first_result=verdict(False),
    )
    assert not judged.supported
    assert judged.failure["stage"] == "rewrite"
    assert llm.generate.await_count == 2 and checker.check.await_count == 0


@pytest.mark.asyncio
async def test_exhausted_validation_is_explicit_auditable_and_does_not_expose_rejected_drafts():
    rejected = verdict(False, missing_conditions=["必须限定数码品类"])
    reply = LLMResponse(rejected.model_dump_json())
    rewrite = LLMResponse('{"answer":"拒绝的候选文本 [digital]","citation_chunk_ids":[]}')
    llm = SimpleNamespace(generate=AsyncMock(side_effect=[reply, rewrite, reply, rewrite, reply]))
    result = await AnswerValidator(llm).validate(
        "超过15天还能退吗",
        {
            "answer": "所有商品都不能退 [digital]",
            "status": "completed",
            "observations": [
                {
                    "name": "search_knowledge",
                    "output": {
                        "success": True,
                        "output": {"chunks": [chunk().model_dump()]},
                    },
                }
            ],
        },
    )
    assert result["status"] == "validation_failed"
    assert result["failure"]["code"] == "ANSWER_VALIDATION_FAILED"
    audit = result["validation"]["attempts"]
    assert len(audit) == 3 and all(not a["verdict"]["supported"] for a in audit)
    assert result["events"][-1]["event"] == "output_validation"
    assert "拒绝的候选文本" not in json.dumps(result["validation"], ensure_ascii=False)
    assert "必须限定数码品类" in json.dumps(result["validation"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_checker_gets_capabilities_and_primary_sources_without_model_drafts_as_evidence():
    llm = SimpleNamespace(generate=AsyncMock(return_value=LLMResponse(verdict().model_dump_json())))
    contracts = [
        {
            "function": {
                "name": "cancel_order",
                "description": "仅created或paid可取消，须确认",
                "parameters": {"required": ["order_id"]},
            }
        }
    ]
    validator = AnswerValidator(llm, tool_contracts=contracts)
    result = await validator.validate(
        "未发货订单怎么关",
        {
            "answer": "请提供订单号，查询后生成待确认提案。",
            "status": "needs_input",
            "observations": [
                {
                    "name": "search_knowledge",
                    "output": {
                        "success": True,
                        "output": {
                            "chunks": [chunk().model_dump()],
                            "answer": "不可信的生成摘要",
                        },
                    },
                },
                {
                    "name": "ask_user",
                    "output": {"answer": "虚构的用户资格", "missing_fields": ["order_id"]},
                },
            ],
        },
    )
    prompt = llm.generate.await_args.args[0][1]["content"]
    assert "created或paid" in prompt and "order_id" in prompt
    assert "不可信的生成摘要" not in prompt and "虚构的用户资格" not in prompt
    assert prompt.count(chunk().content) == 1
    assert result["status"] == "needs_input"
    # A capability declaration still cannot prove an action happened.
    result = await validator.validate(
        "查一下",
        {
            "answer": "退款已完成",
            "status": "completed",
            "observations": [],
        },
    )
    assert result["status"] == "validation_failed"


@pytest.mark.parametrize("query", ["退货地址给我", "退货寄哪儿", "售后地址"])
def test_address_queries_require_actual_evidence_even_without_question_words(query):
    assert policy_evidence_issue(query) == "policy_without_evidence"


@pytest.mark.parametrize(
    "answer",
    [
        "我来帮您核实情况。请说明订单问题，是未收到货还是退款未到账？",
        "我将为您查询退款进度，请提供订单号。",
        "正在帮您核实退款问题，不代表已经退款。",
    ],
)
def test_investigating_complaint_is_not_an_unexecuted_write_promise(answer):
    from app.harness.completion import completion_issue

    assert completion_issue(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        "我来帮您申请退款。",
        "正在为您处理退款。",
        "我将为您提交退货申请。",
    ],
)
def test_real_unexecuted_action_promises_still_require_a_proposal(answer):
    from app.harness.completion import completion_issue

    assert completion_issue(answer) == "missing_action_proposal"
