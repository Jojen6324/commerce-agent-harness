"""Run-wide budgets shared by RAG, validation, delegation and the model loop."""

import asyncio
import time
from contextlib import contextmanager
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
    delegations: int = 0
    max_delegations: int = 2
    models: int = 0
    tools: int = 0
    reserved_tokens: int = 0
    protected_tokens: int = 0
    protected_models: int = 0
    protected_tools: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("deadline")

    def reserve_model(self, estimate):
        self.check()
        model_limit = max(0, self.max_models - self.protected_models)
        if self.models >= model_limit:
            raise BudgetExceeded(
                "delegation_main_reserve" if self.protected_models else "model_call_budget",
                budget_scope="delegation_reserved_main_budget"
                if self.protected_models
                else "model_calls",
                model_calls=self.models,
                model_call_limit=model_limit,
            )
        projected_tokens = self.usage.total_tokens + self.reserved_tokens + estimate
        if projected_tokens + self.protected_tokens > self.max_tokens:
            raise BudgetExceeded(
                "delegation_main_reserve" if self.protected_tokens else "run_token_budget",
                budget_scope="delegation_reserved_main_budget"
                if self.protected_tokens
                else "run_tokens",
                requested_tokens=estimate,
                projected_tokens=projected_tokens + self.protected_tokens,
                max_total_tokens=self.max_tokens,
                total_tokens=self.usage.total_tokens,
                reserved_tokens_without_usage=self.reserved_tokens,
                protected_tokens=self.protected_tokens,
                model_calls=self.models,
                model_call_limit=max(0, self.max_models - self.protected_models),
            )
        self.models += 1
        # Conservative reservation stays charged even on transport failure.
        self.reserved_tokens += estimate

    def reserve_delegation(self):
        self.check()
        if self.delegations >= self.max_delegations:
            raise BudgetExceeded("delegation_call_budget", budget_scope="delegation_calls")
        self.delegations += 1

    @contextmanager
    def delegation_share(self, remaining_experts):
        """Sequential child allowance; keep both sibling and main capacity protected.

        All calls still charge this task's original budget. Unspent child quota
        remains available to later children and to the main agent.
        """
        saved = self.protected_tokens, self.protected_models, self.protected_tools
        tokens = (
            max(0, self.max_tokens - self.usage.total_tokens - self.reserved_tokens - saved[0])
            // remaining_experts
        )
        models = max(0, self.max_models - self.models - saved[1]) // remaining_experts
        tools = max(0, self.max_tools - self.tools - saved[2]) // remaining_experts
        if tokens < 4000 or models < 2 or tools < 1:
            raise BudgetExceeded(
                "delegation_share_unavailable", budget_scope="delegation_allocation"
            )
        self.protected_tokens = (
            self.max_tokens - self.usage.total_tokens - self.reserved_tokens - tokens
        )
        self.protected_models = self.max_models - self.models - models
        self.protected_tools = self.max_tools - self.tools - tools
        try:
            yield {"tokens": tokens, "model_calls": models, "tool_calls": tools}
        finally:
            self.protected_tokens, self.protected_models, self.protected_tools = saved

    def reserve_tool(self):
        self.check()
        tool_limit = max(0, self.max_tools - self.protected_tools)
        if self.tools >= tool_limit:
            raise BudgetExceeded(
                "delegation_main_reserve" if self.protected_tools else "tool_budget",
                budget_scope="delegation_reserved_main_budget"
                if self.protected_tools
                else "tool_calls",
                tool_calls=self.tools,
                tool_call_limit=tool_limit,
            )
        self.tools += 1


current_budget: ContextVar[RunBudget | None] = ContextVar("agent_budget", default=None)


class BudgetedProvider:
    def __init__(self, provider, context_tokens=32_000):
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
