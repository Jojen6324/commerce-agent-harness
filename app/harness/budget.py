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
class RoleAllowance:
    max_tokens: int | None = None
    max_models: int | None = None
    max_tools: int | None = None
    reserve_tokens: int = 0
    reserve_models: int = 0
    reserve_tools: int = 0
    tokens: int = 0
    models: int = 0
    tools: int = 0
    reserved: int = 0


role_allowance: ContextVar[RoleAllowance | None] = ContextVar("role_allowance", default=None)


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
        local = role_allowance.get()
        if local:
            if (local.max_models is not None and local.models >= local.max_models) or (
                local.max_tokens is not None
                and local.tokens + local.reserved + estimate > local.max_tokens
            ):
                raise BudgetExceeded("role_model_budget", budget_scope="role")
            if (local.reserve_models and self.models >= self.max_models - local.reserve_models) or (
                local.reserve_tokens
                and self.usage.total_tokens + self.reserved_tokens + estimate + local.reserve_tokens
                > self.max_tokens
            ):
                raise BudgetExceeded(
                    "delegation_main_reserve", budget_scope="request_reserved_main"
                )
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
        from app.harness.telemetry import current_ledger

        ledger = current_ledger.get()
        if ledger:
            ledger.record("model_reservation", elapsed_ms=0, requested_tokens=estimate)
        if local:
            local.models += 1
            local.reserved += estimate

    def snapshot(self):
        return {
            "models": self.models,
            "tools": self.tools,
            "delegations": self.delegations,
            "reserved_tokens": self.reserved_tokens,
            "usage": self.usage.model_dump(),
            "deadline_epoch": time.time() + max(0, self.deadline - time.monotonic()),
        }

    def restore(self, state):
        # Restoring never grants a fresh budget or a fresh deadline.
        for name in ("models", "tools", "delegations", "reserved_tokens"):
            setattr(self, name, max(getattr(self, name), state.get(name, 0)))
        previous = TokenUsage.model_validate(state.get("usage", {}))
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            setattr(self.usage, name, max(getattr(self.usage, name), getattr(previous, name)))
        if "deadline_epoch" in state:
            self.deadline = min(
                self.deadline, time.monotonic() + state["deadline_epoch"] - time.time()
            )

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
        local = role_allowance.get()
        if local:
            if local.max_tools is not None and local.tools >= local.max_tools:
                raise BudgetExceeded("role_tool_budget", budget_scope="role")
            if local.reserve_tools and self.tools >= self.max_tools - local.reserve_tools:
                raise BudgetExceeded(
                    "delegation_main_reserve", budget_scope="request_reserved_main"
                )
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
        if local:
            local.tools += 1


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
        from app.harness.observability import span
        from app.harness.telemetry import current_ledger

        ledger = current_ledger.get()
        started = time.perf_counter()
        try:
            with span("model.call", provider=type(self.provider).__name__):
                async with asyncio.timeout(max(0.001, budget.deadline - time.monotonic())):
                    response = await self.provider.generate(messages, **kwargs)
        except BaseException as exc:
            if ledger:
                ledger.record(
                    "model",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    success=False,
                    error=type(exc).__name__,
                    reserved_tokens=estimate + output,
                )
            raise
        used = response.usage
        if used.total_tokens:
            budget.reserved_tokens -= estimate + output
            budget.usage.total_tokens += used.total_tokens
            local = role_allowance.get()
            if local:
                local.reserved -= estimate + output
                local.tokens += used.total_tokens
        budget.usage.prompt_tokens += used.prompt_tokens
        budget.usage.completion_tokens += used.completion_tokens
        if ledger:
            ledger.record(
                "model",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                model=response.model,
                **used.model_dump(),
            )
        return response

    async def ping(self):
        return await self.provider.ping()
