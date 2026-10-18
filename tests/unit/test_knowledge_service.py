from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.providers.fake import OfflineLLMProvider
from app.schemas.retrieval import RetrievedChunk
from app.services.knowledge import KnowledgeService


@pytest.mark.asyncio
async def test_full_knowledge_pipeline_preserves_sources():
    chunk = RetrievedChunk(
        chunk_id="c1",
        document_id="d1",
        title="政策",
        content="质量问题可以申请退货。",
        score=1,
        policy_version="v2",
    )
    retriever = AsyncMock()
    retriever.retrieve.return_value = [chunk]
    reranker = AsyncMock()
    reranker.rerank.return_value = [chunk]
    service = KnowledgeService(
        retriever, reranker, OfflineLLMProvider(), Settings(app_env="testing")
    )
    result = await service.answer_question("质量问题能退吗？", complex=True)
    assert result["chunks"][0]["policy_version"] == "v2"
    assert result["citations"][0]["chunk_id"] == "c1"
    assert "质量问题" in result["answer"]
    assert reranker.rerank.await_count >= 1
