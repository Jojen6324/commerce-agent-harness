"""Deterministic checks first, then bounded evidence/tool-based semantic check."""

import json
import re

from app.agents.nodes.faithfulness import self_correct_answer, validation_attempt
from app.guardrails.hallucination import FaithfulnessChecker
from app.harness.receipts import support_receipt
from app.providers.fake import OfflineLLMProvider
from app.retrieval.evidence import cited_refs
from app.schemas.retrieval import RetrievedChunk


def asserted_phrase(answer, phrase):
    """Return true only when a completion phrase is stated affirmatively."""
    for match in re.finditer(re.escape(phrase), answer):
        prefix = answer[max(0, match.start() - 16) : match.start()]
        if re.search(
            r"(?:不代表|不等于|并非|不是|未能|不能(?:证明|确认|声称|说明)?|"
            r"无法(?:证明|确认|声称|说明)?|没有|尚未|并不|未)"
            r"[^，。；！？!?]{0,8}$",
            prefix,
        ):
            continue
        return True
    return False


class AnswerValidator:
    def __init__(self, llm, *, tool_contracts=None):
        self.llm = llm
        self.tool_contracts = [
            {
                "name": item["function"]["name"],
                "description": item["function"]["description"],
                "required_parameters": item["function"].get("parameters", {}).get("required", []),
            }
            for item in (tool_contracts or [])
        ]

    async def validate(self, query, result, *, allow_correction=True):
        if result["status"] not in ("completed", "needs_input"):
            return result
        answer = result["answer"]

        def reject(reason, message, **details):
            validation = {
                "status": "rejected",
                "attempts": [{"stage": "deterministic", "reason": reason, **details}],
            }
            return {
                **result,
                "answer": message,
                "status": "validation_failed",
                "failure": {
                    "code": "ANSWER_VALIDATION_FAILED",
                    "stage": "output_validation",
                    "reason": reason,
                    "retryable": False,
                },
                "validation": validation,
                "events": [*result.get("events", []), {"event": "output_validation", **validation}],
            }

        observations = result.get("observations", [])
        grounded_observations = [
            observation for observation in observations if observation.get("name") != "ask_user"
        ]
        if result["status"] == "needs_input" and not grounded_observations:
            return result
        chunks = {}
        tool_facts = []
        verified = []
        for observation in observations:
            # A model's clarification draft is not independent evidence for
            # its own claims. Failed tools cannot supply policy facts either.
            if observation["name"] == "ask_user":
                continue
            if observation["output"].get("success") is False:
                tool_facts.append({"name": observation["name"], "result": {"success": False}})
                continue
            data = observation["output"].get("output", observation["output"])
            for c in data.get("chunks", []):
                chunks[c["chunk_id"]] = RetrievedChunk.model_validate(c)
            if data.get("validated") and data.get("answered_question") == query:
                verified.append(data.get("answer"))
            # Pass policy once, with provenance, rather than once as evidence
            # and again as a long JSON tool result. Generated summaries are not
            # additional primary sources.
            facts = {
                k: v
                for k, v in data.items()
                if k not in ("chunks", "answer", "summary", "validation")
            }
            tool_facts.append({"name": observation["name"], "result": facts})
        known_refs = (
            set(chunks)
            | {o["name"] for o in observations}
            | {o.get("call_id", "") for o in observations}
        )
        cited = cited_refs(answer)
        unknown = cited - known_refs
        pending = any(o["output"].get("pending_action") for o in observations)
        completion_claim = any(
            asserted_phrase(answer, word)
            for word in ("退款已完成", "已到账", "退款成功", "已取消", "已提交")
        )
        refund_complete = any(
            asserted_phrase(answer, word) for word in ("退款已完成", "已到账", "退款成功")
        )
        has_settled = any(
            d["result"].get("status") == "refunded"
            or any(r.get("status") == "refunded" for r in d["result"].get("refunds", []))
            for d in tool_facts
        )
        if refund_complete and not has_settled:
            return reject(
                "unverified_refund_completion",
                "当前记录不能证明退款已到账；申请提交与退款完成是不同状态。",
            )
        # A requested/pending action can never support a completion claim.
        if unknown or (pending and completion_claim):
            result["events"].append(
                {
                    "event": "validation_failed",
                    "reason": "unknown_citation_or_action_claim",
                    "references": sorted(unknown),
                }
            ) if "events" in result else None
            return reject(
                "unknown_citation_or_action_claim",
                "现有证据不足以支持该结论，请核对原文或补充信息。",
                unknown_references=sorted(unknown),
                pending_completion_claim=bool(pending and completion_claim),
            )
        provider = getattr(self.llm, "provider", self.llm)
        if any(
            o["output"].get("success") and support_receipt(o["output"].get("output", {})) == answer
            for o in observations
        ):
            # Exact server-rendered database receipt, no free-form claims. The
            # dispatch has already committed it; another LLM cannot add assurance.
            return result
        if not observations:
            if completion_claim:
                return reject(
                    "unverified_action_completion", "尚未执行或核实业务操作，无法确认已完成。"
                )
            return result
        if answer in verified and len(observations) == 1:
            return result
        if not allow_correction and result["status"] == "completed" and chunks and not cited:
            return reject(
                "missing_evidence_citation",
                "答复缺少可核对的证据引用。",
                available_references=sorted(chunks),
            )
        if isinstance(provider, OfflineLLMProvider):
            return result
        checker = FaithfulnessChecker(self.llm)
        text = (
            "实际调用结果（订单状态与执行结果只能由这里证明）：\n"
            + json.dumps(tool_facts, ensure_ascii=False)
            + "\n\n应用声明的可用办理能力 available_tools（独立于知识库的接口契约；"
            "可证明可申请的路径、参数与状态条件，不能证明已执行或已获批准）：\n"
            + "\n".join(
                f"[capability:{item['name']}] {item['description']} "
                f"必填参数：{','.join(item['required_parameters'])}"
                for item in self.tool_contracts
            )
        )
        audit: list[dict] = []

        def finish(value: dict, verdict_status: str) -> dict:
            validation = {"status": verdict_status, "attempts": audit}
            value = {**value, "validation": validation}
            value["events"] = [
                *value.get("events", []),
                {"event": "output_validation", **validation},
            ]
            return value

        judged = await checker.check(
            query=query, answer=answer, chunks=list(chunks.values()), tool_results_text=text
        )
        audit.append(validation_attempt(answer, judged, "initial"))
        if judged.rationale == "checker_unavailable":
            return finish(
                {
                    **result,
                    "answer": "答案校验服务暂不可用，本轮无法提供已核验结论。请核对引用原文或稍后重试。",
                    "status": "failed",
                    "failure": judged.failure
                    or {
                        "code": "VALIDATION_UNAVAILABLE",
                        "reason": "checker_unavailable",
                        "retryable": True,
                    },
                },
                "unavailable",
            )
        if judged.supported:
            return finish(result, "passed")
        if not allow_correction:
            return finish(
                {
                    **result,
                    "answer": "现有证据不足，暂时无法给出可靠答复。请补充信息或转人工。",
                    "status": "validation_failed",
                    "failure": {
                        "code": "ANSWER_VALIDATION_FAILED",
                        "stage": "output_validation",
                        "reason": "candidate_rejected",
                        "retryable": False,
                    },
                },
                "rejected",
            )
        rewritten, judge, _ = await self_correct_answer(
            llm=self.llm,
            checker=checker,
            query=query,
            answer=answer,
            chunks=list(chunks.values()),
            tool_results_text=text,
            first_result=judged,
            allowed_refs=known_refs,
            audit=audit,
        )
        accepted = judge.supported and not (cited_refs(rewritten) - known_refs)
        if not accepted:
            rewritten = "现有证据不足，暂时无法给出可靠答复。请补充信息或转人工。"
        failure = judge.failure or (
            None
            if accepted
            else {
                "code": "ANSWER_VALIDATION_FAILED",
                "stage": "output_validation",
                "reason": "bounded_correction_exhausted",
                "retryable": False,
            }
        )
        return finish(
            {
                **result,
                "answer": rewritten,
                "failure": failure,
                "status": result["status"]
                if accepted
                else "failed"
                if judge.failure
                else "validation_failed",
            },
            "passed" if accepted else "unavailable" if judge.failure else "rejected",
        )
