from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.nodes.faithfulness import faithfulness_node, self_correct_answer
from app.core.enums import RetrievalStrategy
from app.guardrails.hallucination import FaithfulnessChecker, FaithfulnessResult
from app.providers.base import LLMResponse
from app.schemas.common import TokenUsage
from app.schemas.retrieval import RetrievedChunk


class _SequenceLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def ping(self) -> bool:
        return True

    async def generate(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            content = '{"answer":"七天无理由不含定制","citation_chunk_ids":[]}'
        else:
            content = '{"supported":true,"hallucinated_spans":[],"rationale":"ok"}'
        return LLMResponse(content=content, usage=TokenUsage(total_tokens=4), model="fake")


@pytest.mark.asyncio
async def test_self_correct_rewrites_unsupported_answer() -> None:
    llm = _SequenceLLM()
    checker = FaithfulnessChecker(llm)
    chunk = RetrievedChunk(
        chunk_id="c1",
        document_id="return_policy_0",
        title="退货",
        content="定制商品不适用七天无理由。",
        score=1.0,
        rank=1,
        source="bm25",
    )
    from app.guardrails.hallucination import FaithfulnessResult

    first = FaithfulnessResult(
        supported=False, hallucinated_spans=["随时退"], rationale="unsupported"
    )
    answer, judged, corrected = await self_correct_answer(
        llm=llm,
        checker=checker,
        query="定制杯能七天无理由吗",
        answer="都可以随时退",
        chunks=[chunk],
        tool_results_text="",
        first_result=first,
    )
    assert corrected is True
    assert judged.supported is True
    assert "定制" in answer


@pytest.mark.asyncio
async def test_rewrite_replaces_original_citations_with_its_actual_sources():
    chunks = [
        RetrievedChunk(
            chunk_id=ref,
            document_id="policy",
            title="规则",
            content="相关政策",
            score=1,
        )
        for ref in ("old", "correct")
    ]
    checker = SimpleNamespace(
        check=AsyncMock(
            side_effect=[
                FaithfulnessResult(supported=False, rationale="适用来源错误"),
                FaithfulnessResult(supported=True),
            ]
        )
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=LLMResponse(
                '{"answer":"按当前适用规则办理","citation_chunk_ids":["correct"]}'
            )
        )
    )
    result = await faithfulness_node(
        {
            "user_query": "如何办理",
            "strategy": RetrievalStrategy.SINGLE,
            "retrieved_chunks": chunks,
            "final_answer": "旧答复 [old]",
        },
        checker,
        llm,
    )
    assert "[correct]" in result["final_answer"]
    assert [citation.chunk_id for citation in result["citations"]] == ["correct"]
