"""Request-local accounting adapted from evoagent.telemetry.ExecutionLedger.

Updates contain no awaits: reservations and ledger writes are atomic within the
service event loop. ContextVars isolate requests and propagate into child Tasks.
"""

import time
from contextvars import ContextVar
from dataclasses import dataclass, field

active_role: ContextVar[str] = ContextVar("commerce_role", default="main")


@dataclass
class ExecutionLedger:
    trace_id: str = ""
    input_price: float | None = None
    output_price: float | None = None
    started: float = field(default_factory=time.monotonic)
    calls: list[dict] = field(default_factory=list)

    def record(self, kind, *, elapsed_ms, success=True, **detail):
        from app.harness.agentic_core import active_run_id

        self.calls.append(
            {
                "kind": kind,
                "role": active_role.get(),
                "run_id": active_run_id.get(),
                "trace_id": self.trace_id,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": success,
                **detail,
            }
        )

    def summary(self):
        models = [c for c in self.calls if c["kind"] == "model"]
        cost = None
        if self.input_price is not None and self.output_price is not None:
            cost = (
                sum(
                    c.get("prompt_tokens", 0) * self.input_price
                    + c.get("completion_tokens", 0) * self.output_price
                    for c in models
                )
                / 1e6
            )
        return {
            "calls": self.calls,
            "cost": cost,
            "cost_note": "Unpriced unless explicit per-million prices are configured.",
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 2),
        }


current_ledger: ContextVar[ExecutionLedger | None] = ContextVar("commerce_ledger", default=None)
