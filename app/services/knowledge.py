"""Knowledge retrieval and the legacy explicit policy-answer adapter.

The production ``search_knowledge`` operation is deliberately retrieval-only:
the caller (an BoundedRole agent) decides whether to issue another query.  The
older ``answer_question`` method remains as an explicit compatibility helper,
but is not registered as a runtime tool.
"""

from typing import cast

from app.agents.nodes.faithfulness import faithfulness_node
from app.agents.nodes.generate import generate_node
from app.agents.nodes.retrieval import retrieval_node
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.enums import RetrievalStrategy
from app.guardrails.hallucination import FaithfulnessChecker


class KnowledgeService:
    def __init__(self, retriever, reranker, llm, settings: Settings):
        self.retriever, self.reranker = retriever, reranker
        self.llm, self.settings = llm, settings

    async def retrieve_evidence(self, query: str, *, complex: bool = False) -> dict:
        strategy = RetrievalStrategy.MULTI if complex else RetrievalStrategy.SINGLE
        result = await retrieval_node(
            {"user_query": query, "strategy": strategy},
            retriever=self.retriever,
            reranker=self.reranker,
            settings=self.settings,
        )
        return {
            "chunks": [c.model_dump(mode="json") for c in result["retrieved_chunks"]],
            "strategy": strategy.value,
            # Kept for response compatibility. Rewrites are now explicit Agent
            # tool calls, never hidden inside retrieval.
            "rewritten_queries": [query],
        }

    async def answer_question(
        self, query: str, *, complex: bool = False, user_question: str | None = None
    ) -> dict:
        from app.schemas.retrieval import RetrievedChunk

        evidence = await self.retrieve_evidence(query, complex=complex)
        state: AgentState = {
            "user_query": user_question or query,
            "strategy": RetrievalStrategy(evidence["strategy"]),
            "retrieved_chunks": [RetrievedChunk.model_validate(c) for c in evidence["chunks"]],
        }
        state.update(cast(AgentState, await generate_node(state, self.llm)))
        state.update(
            cast(
                AgentState, await faithfulness_node(state, FaithfulnessChecker(self.llm), self.llm)
            )
        )
        extra = state.get("extra") or {}
        return {
            **evidence,
            "answered_question": user_question or query,
            "answer": state["final_answer"],
            "citations": [c.model_dump() for c in state.get("citations", [])],
            "validated": not state.get("refused", False) and not extra.get("degraded", False),
            "degraded": extra.get("degraded", False),
            "degradation_reason": extra.get("degradation_reason"),
            "failure": (extra.get("faithfulness") or {}).get("failure"),
            "generation_failure": extra.get("generation_failure"),
            "validation": extra.get("validation_attempts", []),
        }
