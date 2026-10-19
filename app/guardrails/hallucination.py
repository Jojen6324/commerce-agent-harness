"""Faithfulness scoring against retrieved evidence."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.core.exceptions import LLMOutputError, LLMProviderError
from app.prompts.faithfulness import FAITHFULNESS_SYSTEM, FAITHFULNESS_USER
from app.providers.base import LLMProvider
from app.providers.json_parser import parse_model
from app.retrieval.evidence import evidence_text
from app.schemas.retrieval import RetrievedChunk


class FaithfulnessResult(BaseModel):
    supported: bool
    hallucinated_spans: list[str] = Field(default_factory=list)
    rationale: str = ""
    failure: dict[str, Any] | None = None


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
        evidence = "\n\n".join(evidence_text(c) for c in chunks[:8]) or "(none)"
        try:
            response = await self._llm.generate(
                [
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
                ],
                temperature=0.0,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
            return parse_model(response.content, FaithfulnessResult)
        except (LLMProviderError, LLMOutputError) as exc:
            return FaithfulnessResult(
                supported=False,
                rationale="checker_unavailable",
                failure={
                    "code": exc.error_code.value,
                    "reason": exc.message,
                    "retryable": exc.retryable,
                    "stage": "faithfulness",
                },
            )
