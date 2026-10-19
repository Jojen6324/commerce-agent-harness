"""Run-wide budgets shared by RAG, validation, delegation and the model loop."""

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.memory.budget import estimate_tokens
from app.schemas.common import TokenUsage


class BudgetExceeded(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details
        for name, value in details.items():
            setattr(self, name, value)


@dataclass
class RunBudget:
    deadline: float
    max_models: int = 24
    max_tools: int = 16
    max_tokens: int = 80_000
    models: int = 0
    tools: int = 0
    reserved_tokens: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("deadline")

    def reserve_model(self, estimate):
        self.check()
        if self.models >= self.max_models:
            raise BudgetExceeded(
                "model_call_budget",
                budget_scope="model_calls",
                model_calls=self.models,
                model_call_limit=self.max_models,
            )
        projected_tokens = self.usage.total_tokens + self.reserved_tokens + estimate
        if projected_tokens > self.max_tokens:
            raise BudgetExceeded(
                "run_token_budget",
                budget_scope="run_tokens",
                requested_tokens=estimate,
                projected_tokens=projected_tokens,
                max_total_tokens=self.max_tokens,
                total_tokens=self.usage.total_tokens,
                reserved_tokens_without_usage=self.reserved_tokens,
                model_calls=self.models,
                model_call_limit=self.max_models,
            )
        self.models += 1
        # Conservative reservation stays charged even on transport failure.
        self.reserved_tokens += estimate

    def reserve_tool(self):
        self.check()
        if self.tools >= self.max_tools:
            raise BudgetExceeded("tool_budget")
        self.tools += 1


current_budget: ContextVar[RunBudget | None] = ContextVar("agent_budget", default=None)


class BudgetedProvider:
    def __init__(self, provider, context_tokens=16000):
        self.provider = provider
        self.context_tokens = context_tokens

    async def generate(self, messages, **kwargs):
        import json

        budget = current_budget.get()
        estimate = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        estimate += estimate_tokens(json.dumps(kwargs.get("tools", []), ensure_ascii=False))
        output = kwargs.get("max_tokens", 1024)
        if estimate + output > self.context_tokens:
            raise BudgetExceeded(
                "context_budget",
                budget_scope="model_context_tokens",
                context_tokens=estimate + output,
                context_limit_tokens=self.context_tokens,
                requested_output_tokens=output,
            )
        if budget is None:
            return await self.provider.generate(messages, **kwargs)
        budget.reserve_model(estimate + output)
        async with asyncio.timeout(max(0.001, budget.deadline - time.monotonic())):
            response = await self.provider.generate(messages, **kwargs)
        used = response.usage
        if used.total_tokens:
            budget.reserved_tokens -= estimate + output
            budget.usage.total_tokens += used.total_tokens
        budget.usage.prompt_tokens += used.prompt_tokens
        budget.usage.completion_tokens += used.completion_tokens
        return response

    async def ping(self):
        return await self.provider.ping()
