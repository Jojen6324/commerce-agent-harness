"""Self-RAG faithfulness check and answer self-correction."""

from __future__ import annotations

from app.agents.nodes.generate import AnswerPayload, _extractive_fallback
from app.agents.state import AgentState
from app.core.enums import RetrievalStrategy
from app.core.exceptions import LLMOutputError, LLMProviderError
from app.guardrails.hallucination import FaithfulnessChecker, FaithfulnessResult
from app.observability.metrics import HALLUCINATION_FLAGS
from app.prompts.faithfulness import SELF_CORRECT_SYSTEM, SELF_CORRECT_USER
from app.providers.base import LLMProvider
from app.providers.fake import OfflineLLMProvider
from app.providers.json_parser import parse_model
from app.retrieval.evidence import cited_refs, evidence_text
from app.schemas.retrieval import Citation, RetrievedChunk


async def self_correct_answer(
    *,
    llm: LLMProvider,
    checker: FaithfulnessChecker,
    query: str,
    answer: str,
    chunks: list[RetrievedChunk],
    tool_results_text: str,
    first_result: FaithfulnessResult,
) -> tuple[str, FaithfulnessResult, bool]:
    """LLM rewrite once, then extractive fallback. Returns (answer, judge, corrected)."""
    evidence = "\n\n".join(evidence_text(c) for c in chunks[:6]) or "(无检索结果)"
    try:
        response = await llm.generate(
            [
                {"role": "system", "content": SELF_CORRECT_SYSTEM},
                {
                    "role": "user",
                    "content": SELF_CORRECT_USER.format(
                        query=query,
                        answer=answer,
                        critique=first_result.rationale
                        or ",".join(first_result.hallucinated_spans),
                        evidence=evidence,
                        tool_results=tool_results_text or "(无)",
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        payload = parse_model(response.content, AnswerPayload)
        known_refs = {chunk.chunk_id for chunk in chunks}
        if set(payload.citation_chunk_ids) - known_refs:
            raise LLMOutputError("rewrite referenced unavailable evidence")
        rewritten = payload.answer
        missing_refs = set(payload.citation_chunk_ids) - cited_refs(rewritten)
        if missing_refs:
            rewritten += " " + " ".join(f"[{ref}]" for ref in sorted(missing_refs))
        judged = await checker.check(
            query=query,
            answer=rewritten,
            chunks=chunks,
            tool_results_text=tool_results_text,
        )
        if judged.supported:
            return rewritten, judged, True
    except (LLMProviderError, LLMOutputError):
        pass
    patched = _extractive_fallback(query, evidence, tool_results_text)
    judged = await checker.check(
        query=query,
        answer=patched,
        chunks=chunks,
        tool_results_text=tool_results_text,
    )
    return patched, judged, True


async def faithfulness_node(
    state: AgentState, checker: FaithfulnessChecker, llm: LLMProvider
) -> dict:
    answer = state.get("final_answer") or ""
    if (
        state.get("refused")
        or state.get("pending_action")
        or (state.get("extra") or {}).get("missing_order_id")
    ):
        return {}
    chunks = state.get("retrieved_chunks") or []
    tools = state.get("tool_results") or []
    if isinstance(llm, OfflineLLMProvider) or state.get("strategy") == RetrievalStrategy.DIRECT:
        return {}
    if state.get("strategy") in {RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI} and not chunks:
        return {
            "final_answer": "现有资料不足，暂时无法可靠回答。请补充具体商品或政策信息。",
            "citations": [],
            "refused": True,
        }
    tool_text = str([t.model_dump() for t in tools])
    result = await checker.check(
        query=state["user_query"],
        answer=answer,
        chunks=chunks,
        tool_results_text=tool_text,
    )
    extra = {
        **(state.get("extra") or {}),
        "faithfulness": result.model_dump(),
        "self_corrected": False,
    }
    if result.supported:
        return {"extra": extra}
    HALLUCINATION_FLAGS.inc()
    if result.rationale == "checker_unavailable":
        evidence = "\n\n".join(evidence_text(c) for c in chunks[:6])
        fallback_answer = _extractive_fallback(state["user_query"], evidence, tool_text)
        extra["degraded"] = True
        extra["degradation_reason"] = "checker_unavailable"
        citations = [
            Citation(
                document_id=chunk.document_id,
                title=chunk.title,
                chunk_id=chunk.chunk_id,
                section=chunk.section,
            )
            for chunk in chunks[:1]
        ]
        return {
            "final_answer": fallback_answer,
            "citations": citations,
            "extra": extra,
        }
    patched, judged, corrected = await self_correct_answer(
        llm=llm,
        checker=checker,
        query=state["user_query"],
        answer=answer,
        chunks=chunks,
        tool_results_text=tool_text,
        first_result=result,
    )
    extra = {
        **(state.get("extra") or {}),
        "faithfulness": judged.model_dump(),
        "self_corrected": corrected,
    }
    if not judged.supported:
        return {
            "final_answer": "现有证据不足，暂时无法给出可靠答复。请补充信息或转人工。",
            "citations": [],
            "refused": True,
            "extra": extra,
        }
    used_refs = cited_refs(patched)
    return {
        "final_answer": patched,
        "citations": [
            Citation(
                document_id=chunk.document_id,
                title=chunk.title,
                chunk_id=chunk.chunk_id,
                section=chunk.section,
            )
            for chunk in chunks
            if chunk.chunk_id in used_refs
        ],
        "extra": extra,
    }
