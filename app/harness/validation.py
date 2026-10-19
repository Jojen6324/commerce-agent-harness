"""Deterministic checks first, then bounded evidence/tool-based semantic check."""

import json
import re

from app.agents.nodes.faithfulness import self_correct_answer
from app.guardrails.hallucination import FaithfulnessChecker
from app.providers.fake import OfflineLLMProvider
from app.schemas.retrieval import RetrievedChunk


def cited_refs(answer):
    refs = set()
    for bracket in re.findall(r"\[([^\]\n]+)\]", answer):
        for ref in re.split(r"[,，;；\s]+", bracket):
            if re.fullmatch(r"[A-Za-z0-9_-]+", ref):
                refs.add(ref)
    return refs


class AnswerValidator:
    def __init__(self, llm):
        self.llm = llm

    async def validate(self, query, result):
        if result["status"] != "completed":
            return result
        answer = result["answer"]
        observations = result.get("observations", [])
        chunks = {}
        tool_facts = []
        verified = []
        for observation in observations:
            data = observation["output"].get("output", observation["output"])
            for c in data.get("chunks", []):
                chunks[c["chunk_id"]] = RetrievedChunk.model_validate(c)
            if data.get("validated"):
                verified.append(data.get("answer"))
            tool_facts.append({"name": observation["name"], "result": data})
        known_refs = (
            set(chunks)
            | {o["name"] for o in observations}
            | {o.get("call_id", "") for o in observations}
        )
        cited = cited_refs(answer)
        unknown = cited - known_refs
        pending = any(o["output"].get("pending_action") for o in observations)
        completion_claim = any(
            w in answer for w in ("退款已完成", "已到账", "退款成功", "已取消", "已提交")
        )
        refund_complete = any(w in answer for w in ("退款已完成", "已到账", "退款成功"))
        has_settled = any(
            d["result"].get("status") == "refunded"
            or any(r.get("status") == "refunded" for r in d["result"].get("refunds", []))
            for d in tool_facts
        )
        if refund_complete and not has_settled:
            return {
                **result,
                "answer": "当前记录不能证明退款已到账；申请提交与退款完成是不同状态。",
                "status": "validation_failed",
            }
        # A requested/pending action can never support a completion claim.
        if unknown or (pending and completion_claim):
            result["events"].append(
                {
                    "event": "validation_failed",
                    "reason": "unknown_citation_or_action_claim",
                    "references": sorted(unknown),
                }
            ) if "events" in result else None
            return {
                **result,
                "answer": "现有证据不足以支持该结论，请核对原文或补充信息。",
                "status": "validation_failed",
            }
        provider = getattr(self.llm, "provider", self.llm)
        if not observations:
            if completion_claim:
                return {
                    **result,
                    "answer": "尚未执行或核实业务操作，无法确认已完成。",
                    "status": "validation_failed",
                }
            return result
        if answer in verified and len(observations) == 1:
            return result
        if isinstance(provider, OfflineLLMProvider):
            return result
        checker = FaithfulnessChecker(self.llm)
        text = json.dumps(tool_facts, ensure_ascii=False)
        judged = await checker.check(
            query=query, answer=answer, chunks=list(chunks.values()), tool_results_text=text
        )
        if judged.supported:
            return result
        rewritten, judge, _ = await self_correct_answer(
            llm=self.llm,
            checker=checker,
            query=query,
            answer=answer,
            chunks=list(chunks.values()),
            tool_results_text=text,
            first_result=judged,
        )
        if not judge.supported or cited_refs(rewritten) - known_refs:
            rewritten = "现有证据不足，暂时无法给出可靠答复。请补充信息或转人工。"
        return {
            **result,
            "answer": rewritten,
            "status": "completed" if judge.supported else "validation_failed",
        }
