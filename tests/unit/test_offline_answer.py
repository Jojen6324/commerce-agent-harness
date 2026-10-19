import pytest

from app.agents.nodes.generate import generate_node
from app.core.enums import RetrievalStrategy
from app.providers.fake import OfflineLLMProvider
from app.schemas.retrieval import RetrievedChunk


@pytest.mark.asyncio
async def test_offline_answer_quotes_evidence_instead_of_test_json() -> None:
    chunk = RetrievedChunk(
        chunk_id="chunk-1",
        document_id="policy-1",
        title="退货规则",
        content="定制商品不适用七天无理由退货；存在质量问题时可申请售后。",
        score=1.0,
    )
    result = await generate_node(
        {
            "user_query": "定制商品有质量问题还能退吗？",
            "strategy": RetrievalStrategy.MULTI,
            "retrieved_chunks": [chunk],
        },
        OfflineLLMProvider(),
    )
    assert "质量问题" in result["final_answer"]
    assert "label" not in result["final_answer"]
    assert result["citations"][0].chunk_id == "chunk-1"
