from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.providers.fake import OfflineLLMProvider
from app.schemas.retrieval import RetrievedChunk
from app.services.knowledge import KnowledgeService
from app.tools.base import ToolContext
from app.tools.knowledge_service import EvidenceTool


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


@pytest.mark.asyncio
async def test_production_search_is_single_pass_pure_retrieval_with_complete_sources():
    chunk = RetrievedChunk(
        chunk_id="policy-7d-2",
        document_id="returns-v3",
        title="退换货政策",
        content="耳机在未拆封时可以申请七天无理由退货；已激活设备除外。",
        score=0.91,
        category="return_policy",
        section="耳机",
        policy_version="v3",
        metadata={"applicable_category": "耳机", "exception": "已激活设备"},
    )
    retriever = AsyncMock()
    retriever.retrieve.return_value = [chunk]
    reranker = AsyncMock()
    reranker.rerank.return_value = [chunk]
    llm = AsyncMock()
    service = KnowledgeService(retriever, reranker, llm, Settings(app_env="testing"))

    result = await EvidenceTool(service).run(
        {"query": "耳机七天无理由，激活后是否例外", "complex": True},
        ToolContext(user_id="u1", session_id="s1", trace_id="t1"),
    )

    assert result.success
    assert result.output["rewritten_queries"] == ["耳机七天无理由，激活后是否例外"]
    assert len(result.output["chunks"]) == 1
    saved = result.output["chunks"][0]
    assert saved["document_id"] == "returns-v3"
    assert saved["policy_version"] == "v3"
    assert saved["section"] == "耳机"
    assert saved["metadata"]["exception"] == "已激活设备"
    retriever.retrieve.assert_awaited_once_with(
        "耳机七天无理由，激活后是否例外",
        top_k=Settings(app_env="testing").retrieval_top_k_complex,
    )
    reranker.rerank.assert_awaited_once()
    llm.generate.assert_not_awaited()
