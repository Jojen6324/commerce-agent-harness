"""Pure hybrid retrieval node used by the knowledge service.

The optional ``rewriter`` and ``self_rag`` parameters are accepted for callers
of the legacy graph API, but are intentionally not invoked. Query planning and
additional retrieval turns belong to the AgentRunner loop.
"""

from __future__ import annotations

import time

from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import DocumentReranker
from app.schemas.common import LatencyBreakdown
from app.schemas.retrieval import RetrievedChunk


async def retrieval_node(
    state: AgentState,
    *,
    retriever: HybridRetriever,
    reranker: DocumentReranker,
    rewriter=None,
    self_rag=None,
    settings: Settings,
) -> dict:
    started = time.perf_counter()
    query = state["user_query"]
    strategy = state.get("strategy")
    top_k = (
        settings.retrieval_top_k_complex
        if strategy == RetrievalStrategy.MULTI
        else settings.retrieval_top_k_simple
    )
    candidates = await retriever.retrieve(query, top_k=top_k)
    # Keep explicit deduplication here even if a custom retriever returns
    # duplicate chunk IDs; the source identity is the evidence identity.
    by_id: dict[str, RetrievedChunk] = {}
    for candidate in candidates:
        by_id.setdefault(candidate.chunk_id, candidate)
    chunks = await reranker.rerank(query, list(by_id.values()), top_n=settings.rerank_top_n)
    rewritten = [query]
    latency = state.get("latency") or LatencyBreakdown()
    latency.retrieval_ms = (time.perf_counter() - started) * 1000
    return {"retrieved_chunks": chunks, "rewritten_queries": rewritten, "latency": latency}
