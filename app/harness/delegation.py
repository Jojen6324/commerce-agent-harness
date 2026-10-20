"""Depth-one business delegation over the shared bounded role and executor."""

import asyncio
import json
from contextvars import ContextVar
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field

from app.core.enums import ToolRisk
from app.harness.agentic_core import active_run_id
from app.harness.business_roles import specialist_specs

delegation_event_sink = ContextVar("delegation_event_sink", default=None)


class SpecialistResult(BaseModel):
    status: Literal["completed", "needs_input", "pending_confirmation", "out_of_scope", "failed"]
    summary: str
    facts: list[dict] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    pending_action_id: str | None = None
    child_run_id: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)


class DelegationService:
    def __init__(self, runner, executor, store):
        self.runner, self.executor, self.store = runner, executor, store
        self.specs = specialist_specs(executor.registry, runner.settings)
        self.gate = asyncio.Semaphore(2)

    async def __call__(self, args, context):
        parent = active_run_id.get()
        emit = delegation_event_sink.get()
        if emit:
            await emit(
                {
                    "event": "delegation_dispatched",
                    "parent_run_id": parent,
                    "expert": args.agent,
                    "source": "model",
                }
            )
        spec = self.specs[args.agent]
        if args.independent:
            spec = replace(
                spec,
                tools=frozenset(
                    n
                    for n in spec.tools
                    if n in {"ask_user", "read_artifact"}
                    or self.executor.registry.get(n).risk == ToolRisk.READ
                ),
            )
        async with self.gate:
            result = await self.runner.run(
                args.goal,
                context,
                spec,
                emit=emit,
                summary=json.dumps(
                    {"main_context": args.context, "user_constraints": context.user_messages[-2:]},
                    ensure_ascii=False,
                ),
            )
            if self.store:
                await self.store.record(result, context, parent_run_id=parent)
        observations = result.get("observations", [])
        refs, artifacts, facts, missing = set(), set(), [], []
        for observation in observations:
            output = observation.get("output", {})
            data = output.get("output", output)
            if observation.get("artifact_ref"):
                artifacts.add(observation["artifact_ref"])
            if observation.get("name") == "ask_user":
                missing.extend(data.get("missing_fields", []))
            if output.get("success") is False:
                continue
            refs.update(c["chunk_id"] for c in data.get("chunks", []) if c.get("chunk_id"))
            # Only real observations supply facts. Raw results remain in artifacts
            # and server-side observations, never in the Main model handoff.
            fact = {
                k: v
                for k, v in data.items()
                if k not in {"chunks", "evidence", "answer", "summary"}
            }
            if fact:
                encoded = json.dumps(fact, ensure_ascii=False)
                if len(encoded) > 1200:
                    fact = {
                        "artifact_ref": observation.get("artifact_ref"),
                        "facts_complete": False,
                    }
                facts.append(
                    {
                        "tool": observation["name"],
                        "arguments": observation.get("arguments", {}),
                        "result": fact,
                    }
                )
        pending = result.get("pending_action")
        status = {
            "completed": "completed",
            "needs_input": "needs_input",
            "awaiting_confirmation": "pending_confirmation",
            "denied": "out_of_scope",
        }.get(result.get("status"), "failed")
        envelope = SpecialistResult(
            status=status,
            summary=result.get("answer", "")[:1500],
            facts=facts[-8:],
            evidence_refs=sorted(refs),
            artifact_refs=sorted(artifacts),
            missing_fields=list(dict.fromkeys(missing)),
            pending_action_id=pending.get("action_id") if pending else None,
            child_run_id=result.get("run_id"),
        ).model_dump()
        envelope["facts_complete"] = all(f["result"].get("facts_complete", True) for f in facts)
        envelope["completed_means"] = "本次委派完成；业务是否执行仅由真实工具状态证明。"
        envelope["summary_complete"] = len(result.get("answer", "")) <= 1500
        if pending:
            envelope["pending_action"] = pending
        # Consumed by the runner before preparing Main's tool message. This is
        # the primary-evidence path used by citation and business-state checks.
        envelope["_observations"] = [
            {**o, "child_run_id": result.get("run_id"), "delegated_by": args.agent}
            for o in observations
        ]
        if emit:
            await emit(
                {
                    "event": "delegation_result",
                    "expert": args.agent,
                    "status": status,
                    "child_run_id": result.get("run_id"),
                    "parent_run_id": parent,
                }
            )
        return envelope
