"""Async node/checkpoint/cancellation interface adapted from evoagent.runtime.

No model loop and no second budget controller. A node is never automatically
retried: a write may already have committed before a worker lost its response.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.harness.budget import current_budget
from app.harness.observability import span


@dataclass(frozen=True)
class RuntimeNode:
    name: str
    handler: Callable[[], Awaitable[dict]]
    input_digest: str = ""


class AgentRuntime:
    async def execute(self, node, *, context, run_id, store=None, resume=False):
        budget = current_budget.get()
        if budget:
            budget.check()
        if resume and store:
            cached = await store.load_checkpoint(run_id, node.name, context, node.input_digest)
            if cached:
                if budget:
                    budget.restore(cached["budget"])
                    budget.check()
                from app.harness.telemetry import current_ledger

                ledger = current_ledger.get()
                if ledger and not ledger.calls:
                    ledger.calls.extend(cached["budget"].get("ledger_calls", []))
                return cached["result"]
        with span("agent.node", role=node.name, run_id=run_id, trace_id=context.trace_id):
            try:
                result = await node.handler()
            except asyncio.CancelledError:
                if store:
                    await asyncio.shield(store.cancel_run(run_id, context))
                raise
        if store and hasattr(store, "save_checkpoint"):
            from app.harness.telemetry import current_ledger

            ledger = current_ledger.get()
            state = budget.snapshot() if budget else {}
            state["ledger_calls"] = list(ledger.calls) if ledger else []
            await store.save_checkpoint(
                run_id, node.name, context, result, state, node.input_digest
            )
        return result
