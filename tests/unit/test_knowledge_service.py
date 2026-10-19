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


@pytest.mark.asyncio
async def test_rewritten_search_does_not_replace_original_question(monkeypatch):
    import app.services.knowledge as knowledge
    from app.tools.base import ToolContext
    from app.tools.knowledge_service import PolicyAnswerTool

    original = "签收8天了，普通T恤，无质量问题，只是不喜欢，还能无理由退吗？"
    captured = []

    async def generate(state, llm):
        captured.append(state["user_query"])
        return {"final_answer": "已过七天，不能无理由退货", "citations": []}

    async def check(state, *args):
        return {}

    monkeypatch.setattr(knowledge, "generate_node", generate)
    monkeypatch.setattr(knowledge, "faithfulness_node", check)
    service = KnowledgeService(
        AsyncMock(), AsyncMock(), OfflineLLMProvider(), Settings(app_env="testing")
    )
    service.retrieve_evidence = AsyncMock(
        return_value={"chunks": [], "strategy": "single", "rewritten_queries": ["无理由退货"]}
    )
    result = await PolicyAnswerTool(service).run(
        {"query": "无理由退货 7天 普通T恤"},
        ToolContext(user_id="u", session_id="s", trace_id="t", user_messages=[original]),
    )
    assert captured == [original]
    assert result.output["answered_question"] == original
    service.retrieve_evidence.assert_awaited_once_with("无理由退货 7天 普通T恤", complex=False)
