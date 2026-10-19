"""One complete RAG pipeline shared by agents and policy answers."""

from typing import cast

from app.agents.nodes.faithfulness import faithfulness_node
from app.agents.nodes.generate import generate_node
from app.agents.nodes.retrieval import retrieval_node
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.guardrails.hallucination import FaithfulnessChecker
from app.retrieval.query_rewrite import QueryRewriter
from app.retrieval.self_rag import SelfRAGLoop


class KnowledgeService:
    def __init__(self, retriever, reranker, llm, settings: Settings):
        self.retriever, self.reranker = retriever, reranker
        self.llm, self.settings = llm, settings

    async def retrieve_evidence(self, query: str, *, complex: bool = False) -> dict:
        strategy = RetrievalStrategy.MULTI if complex else RetrievalStrategy.SINGLE
        rewriter = QueryRewriter(self.llm)
        loop = SelfRAGLoop(self.retriever, self.reranker, rewriter, self.llm, self.settings)
        result = await retrieval_node(
            {"user_query": query, "strategy": strategy},
            retriever=self.retriever,
            reranker=self.reranker,
            rewriter=rewriter,
            self_rag=loop,
            settings=self.settings,
        )
        return {
            "chunks": [c.model_dump(mode="json") for c in result["retrieved_chunks"]],
            "strategy": strategy.value,
            "rewritten_queries": result["rewritten_queries"],
        }

    async def answer_question(self, query: str, *, complex: bool = False) -> dict:
        from app.schemas.retrieval import RetrievedChunk

        evidence = await self.retrieve_evidence(query, complex=complex)
        state: AgentState = {
            "user_query": query,
            "strategy": RetrievalStrategy(evidence["strategy"]),
            "retrieved_chunks": [RetrievedChunk.model_validate(c) for c in evidence["chunks"]],
        }
        state.update(cast(AgentState, await generate_node(state, self.llm)))
        state.update(
            cast(
                AgentState, await faithfulness_node(state, FaithfulnessChecker(self.llm), self.llm)
            )
        )
        return {
            **evidence,
            "answer": state["final_answer"],
            "citations": [c.model_dump() for c in state.get("citations", [])],
            "validated": not state.get("refused", False),
        }
