"""Self-RAG faithfulness check and answer self-correction."""

from __future__ import annotations

import hashlib

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


def validation_attempt(answer: str, result: FaithfulnessResult, stage: str) -> dict:
    """Persist the verdict without replaying a rejected answer to the customer."""
    return {
        "stage": stage,
        "candidate_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "verdict": result.model_dump(),
    }


async def self_correct_answer(
    *,
    llm: LLMProvider,
    checker: FaithfulnessChecker,
    query: str,
    answer: str,
    chunks: list[RetrievedChunk],
    tool_results_text: str,
    first_result: FaithfulnessResult,
    allowed_refs: set[str] | None = None,
    audit: list[dict] | None = None,
) -> tuple[str, FaithfulnessResult, bool]:
    """At most two feedback-driven rewrites, using the same complete evidence.

    Rejected output stays rejected. Quoting the first search hit cannot repair
    missing conditions spread over other sources.
    """
    evidence = "\n\n".join(evidence_text(c) for c in chunks) or "(无检索结果)"
    known_refs = {chunk.chunk_id for chunk in chunks} | (allowed_refs or set())
    judged = first_result
    format_error = ""
    last_error = None
    audit = audit if audit is not None else []
    for attempt in range(2):
        critique = judged.rationale
        if judged.hallucinated_spans:
            critique += "；不支持的说法：" + "、".join(judged.hallucinated_spans)
        if judged.missing_conditions:
            critique += "；遗漏的必要条件：" + "、".join(judged.missing_conditions)
        if format_error:
            critique += "；上次改写未被接受：" + format_error
        try:
            response = await llm.generate(
                [
                    {"role": "system", "content": SELF_CORRECT_SYSTEM},
                    {
                        "role": "user",
                        "content": SELF_CORRECT_USER.format(
                            query=query,
                            answer=answer,
                            critique=critique or "请补齐证据范围、问题回答和必要条件。",
                            evidence=evidence,
                            tool_results=tool_results_text or "(无)",
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=1200 if attempt == 0 else 1800,
                response_format={"type": "json_object"},
            )
            if response.finish_reason != "stop":
                raise LLMProviderError(
                    "rewrite response incomplete",
                    retryable=False,
                    finish_reason=response.finish_reason,
                )
            payload = parse_model(response.content, AnswerPayload)
            if not payload.answer.strip():
                raise LLMOutputError("rewrite returned an empty answer")
            if (set(payload.citation_chunk_ids) | cited_refs(payload.answer)) - known_refs:
                raise LLMOutputError("rewrite referenced unavailable evidence")
            answer = payload.answer
            missing_refs = set(payload.citation_chunk_ids) - cited_refs(answer)
            if missing_refs:
                answer += " " + " ".join(f"[{ref}]" for ref in sorted(missing_refs))
            judged = await checker.check(
                query=query,
                answer=answer,
                chunks=chunks,
                tool_results_text=tool_results_text,
            )
            audit.append(validation_attempt(answer, judged, f"rewrite_{attempt + 1}"))
            format_error = ""
            last_error = None
            if judged.supported or judged.failure:
                return answer, judged, True
        except (LLMProviderError, LLMOutputError) as exc:
            last_error = {
                "code": exc.error_code.value,
                "reason": exc.message,
                "retryable": exc.retryable,
                "stage": "rewrite",
                "finish_reason": getattr(exc, "finish_reason", None),
            }
            audit.append(
                {
                    "stage": f"rewrite_{attempt + 1}",
                    "error": {
                        "code": exc.error_code.value,
                        "reason": exc.message,
                        "finish_reason": getattr(exc, "finish_reason", None),
                    },
                }
            )
            if isinstance(exc, LLMProviderError) and exc.finish_reason != "length":
                return (
                    answer,
                    judged.model_copy(
                        update={
                            "supported": False,
                            "rationale": "checker_unavailable",
                            "failure": {
                                "code": exc.error_code.value,
                                "reason": exc.message,
                                "retryable": exc.retryable,
                                "stage": "rewrite",
                            },
                        }
                    ),
                    True,
                )
            format_error = exc.message
    if last_error:
        judged = judged.model_copy(
            update={
                "supported": False,
                "rationale": "checker_unavailable",
                "failure": last_error,
            }
        )
    return answer, judged.model_copy(update={"supported": False}), True


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
    audit = [validation_attempt(answer, result, "initial")]
    extra = {
        **(state.get("extra") or {}),
        "faithfulness": result.model_dump(),
        "self_corrected": False,
        "validation_attempts": audit,
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
        audit=audit,
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
