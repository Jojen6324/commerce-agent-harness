"""Faithfulness scoring against retrieved evidence."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.exceptions import LLMOutputError, LLMProviderError
from app.prompts.faithfulness import FAITHFULNESS_SYSTEM, FAITHFULNESS_USER
from app.providers.base import LLMProvider
from app.providers.json_parser import parse_model
from app.retrieval.evidence import evidence_text
from app.schemas.retrieval import RetrievedChunk


class FaithfulnessResult(BaseModel):
    supported: bool
    claims_grounded: bool
    answers_question: bool
    scope_preserved: bool
    critical_conditions_complete: bool
    hallucinated_spans: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    supporting_chunk_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    failure: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_grounding_and_task_coverage(self) -> FaithfulnessResult:
        """Do not let a positive aggregate verdict override a failed dimension."""
        object.__setattr__(
            self,
            "supported",
            self.supported
            and self.claims_grounded
            and self.answers_question
            and self.scope_preserved
            and self.critical_conditions_complete
            and not self.hallucinated_spans
            and not self.missing_conditions,
        )
        return self


class FaithfulnessChecker:
    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def check(
        self,
        *,
        query: str,
        answer: str,
        chunks: list[RetrievedChunk],
        tool_results_text: str,
    ) -> FaithfulnessResult:
        evidence = "\n\n".join(evidence_text(c) for c in chunks) or "(none)"
        messages = [
            {"role": "system", "content": FAITHFULNESS_SYSTEM},
            {
                "role": "user",
                "content": FAITHFULNESS_USER.format(
                    query=query,
                    answer=answer,
                    evidence=evidence,
                    tool_results=tool_results_text or "(none)",
                ),
            },
        ]
        for attempt in range(2):
            try:
                response = await self._llm.generate(
                    messages,
                    temperature=0.0,
                    # The structured verdict covers four independent checks and
                    # can include several missing conditions. A 512-token cap
                    # repeatedly truncated live DeepSeek responses, turning
                    # otherwise answerable tasks into provider failures.
                    max_tokens=1024 if attempt == 0 else 1536,
                    response_format={"type": "json_object"},
                )
                if response.finish_reason != "stop":
                    raise LLMProviderError(
                        "faithfulness response incomplete",
                        retryable=False,
                        finish_reason=response.finish_reason,
                    )
                return parse_model(response.content, FaithfulnessResult)
            except (LLMProviderError, LLMOutputError) as exc:
                # Format/length recovery only. Authentication errors, rate limits,
                # content filtering and transport failures retain provider policy.
                recoverable = isinstance(exc, LLMOutputError) or (
                    isinstance(exc, LLMProviderError) and exc.finish_reason == "length"
                )
                if attempt == 0 and recoverable:
                    messages = [
                        *messages,
                        {
                            "role": "system",
                            "content": "上次校验JSON未完整生成或格式无效。请重新核对相同证据，输出完整且精简的JSON；理由最多80字。不得默认通过。",
                        },
                    ]
                    continue
                return FaithfulnessResult(
                    supported=False,
                    claims_grounded=False,
                    answers_question=False,
                    scope_preserved=False,
                    critical_conditions_complete=False,
                    rationale="checker_unavailable",
                    failure={
                        "code": exc.error_code.value,
                        "reason": exc.message,
                        "retryable": exc.retryable,
                        "stage": "faithfulness",
                        "finish_reason": getattr(exc, "finish_reason", None),
                        "attempts": attempt + 1,
                    },
                )
        raise AssertionError("unreachable")
