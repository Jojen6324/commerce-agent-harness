"""Conversation persistence, deterministic confirmation and harness streaming."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar

from app.config.settings import Settings
from app.core.exceptions import (
    GuardrailError,
    InvalidRequestError,
    LLMOutputError,
    LLMProviderError,
    RateLimitError,
)
from app.core.ids import new_id
from app.guardrails.injection import detect_prompt_injection
from app.harness.agentic_core import AgentSpec, BoundedRole, active_run_id
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.business_roles import BusinessMode
from app.harness.delegation import (
    DelegationService,
    delegation_event_sink,
)
from app.harness.evidence import confirmed_item_observations
from app.harness.executor import ToolExecutor
from app.harness.validation import AnswerValidator, FaithfulnessVerdict
from app.prompts.faithfulness import CRITIC_AGENT_PROMPT
from app.providers.fake import OfflineLLMProvider
from app.providers.json_parser import parse_model
from app.repositories.runtime import RuntimeStore
from app.repositories.session_repository import SessionRepository
from app.retrieval.evidence import cited_refs
from app.schemas.chat import ActionProposal, ChatRequest, ChatResponse
from app.schemas.common import LatencyBreakdown
from app.schemas.retrieval import Citation
from app.services.knowledge import KnowledgeService
from app.tools.base import ToolContext
from app.tools.factory import build_tool_registry
from app.tools.knowledge_service import EvidenceTool

validation_runtime_context: ContextVar[dict | None] = ContextVar(
    "validation_runtime_context", default=None
)


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        llm,
        retriever,
        reranker,
        session_factory,
        rate_limiter,
        registry=None,
    ):
        self._settings, self._llm = settings, llm
        self._session_factory, self._rate_limiter = session_factory, rate_limiter
        self.registry = registry or build_tool_registry(retriever)
        self.provider = (
            llm
            if isinstance(llm, OfflineLLMProvider)
            else BudgetedProvider(llm, min(settings.agent_context_tokens, 32_000))
        )
        self.knowledge = KnowledgeService(retriever, reranker, self.provider, settings)
        self.registry.register(EvidenceTool(self.knowledge))
        self.executor = ToolExecutor(
            self.registry,
            session_factory,
            settings.agent_tool_timeout,
            settings.agent_knowledge_timeout,
        )
        self.runner = BoundedRole(self.provider, self.executor, settings)
        self.store = RuntimeStore(session_factory, live_ttl=settings.agent_live_state_ttl_seconds)
        self.executor.artifacts = self.store
        from app.harness.business_roles import MAIN_PROMPT

        self.spec = AgentSpec(
            "main",
            frozenset([*self.registry.names(), "read_artifact", "ask_user"]),
            max_steps=settings.max_agent_steps,
            prompt=MAIN_PROMPT,
        )
        self.delegation_mode = (
            BusinessMode.AUTO
            if (settings.agent_delegation_enabled and settings.agent_delegation_policy != "off")
            else BusinessMode.OFF
        )
        self.delegation_enabled = self.delegation_mode is not BusinessMode.OFF
        if self.delegation_enabled:
            self.executor.delegate = DelegationService(self.runner, self.executor, self.store)
            self.spec = AgentSpec(
                "main",
                self.spec.tools | {"delegate_task"},
                prompt=MAIN_PROMPT,
                max_steps=settings.max_agent_steps,
                delegate_agents=("shopping", "order", "aftersales"),
            )
        self.main_spec = self.spec
        self.validator = AnswerValidator(
            self.provider,
            tool_contracts=self.executor.schemas(self.main_spec),
            verifier=self._verify_faithfulness if settings.agent_critic_enabled else None,
            semantic_enabled=settings.agent_critic_enabled,
        )
        self.runner.output_validator = self.validator.validate
        self._locks: dict[str, asyncio.Lock] = {}

    async def chat(self, request: ChatRequest, *, trace_id: str, emit=None) -> ChatResponse:
        if not await self._rate_limiter.allow(f"user:{request.user_id}"):
            raise RateLimitError()
        session_id = request.session_id or new_id("sess_")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise InvalidRequestError("同一会话正在处理请求，请稍后重试。")
        async with lock:
            return await self._chat(request, session_id, trace_id, emit)

    async def _chat(self, request, session_id, trace_id, emit):
        started = time.perf_counter()
        context = ToolContext(
            user_id=request.user_id,
            session_id=session_id,
            trace_id=trace_id,
            idempotency_key=request.idempotency_key,
        )
        async with self._session_factory() as db:
            repo = SessionRepository(db)
            row = await repo.get_or_create(session_id, request.user_id)
            summary = row.summary or ""
            history = await repo.recent_messages(session_id)
            await db.commit()
        context = context.model_copy(
            update={
                "user_messages": [m.content for m in history if m.role == "user"]
                + [request.message]
            }
        )
        budget = RunBudget(
            time.monotonic() + self._settings.agent_deadline_seconds,
            max_models=self._settings.agent_model_calls,
            max_tools=self._settings.max_tool_calls,
            max_tokens=self._settings.agent_total_tokens,
            max_delegations=self._settings.agent_max_delegations,
        )
        facts = await self.store.facts(context)
        from app.harness.business_case import BusinessCases
        from app.harness.telemetry import ExecutionLedger, current_ledger

        cases = BusinessCases(self._settings.agent_live_state_ttl_seconds)
        facts = cases.prepare(facts, request.message)
        context = context.model_copy(
            update={
                "proposals_forbidden": facts["active_case"].get("proposals_forbidden", False),
                "operation_authorized": facts["active_case"].get("operation_authorized", False),
            }
        )
        ledger = ExecutionLedger(trace_id=trace_id)
        ledger_token = current_ledger.set(ledger)
        token = current_budget.set(budget)
        run_id = new_id("run_")
        active_root_token = active_run_id.set(run_id)
        validation_state = {"context": context, "history": history[-4:], "root_run_id": run_id}
        validation_token = validation_runtime_context.set(validation_state)
        run_events = []
        result = {}

        async def record_event(event):
            nonlocal run_id
            event.setdefault("trace_id", trace_id)
            event.setdefault("session_id", session_id)
            event.setdefault("run_id", run_id)
            event.setdefault("parent_run_id", None)
            event.setdefault("agent", "main")
            if event.get("parent_run_id") is None:
                run_id = event.get("run_id", run_id)
                if run_id:
                    validation_state["root_run_id"] = run_id
            run_events.append(event)
            if emit:
                safe_keys = {
                    "event",
                    "run_id",
                    "parent_run_id",
                    "trace_id",
                    "session_id",
                    "agent",
                    "expert",
                    "child_run_id",
                    "tool",
                    "tool_call_id",
                    "elapsed_ms",
                    "status",
                    "error_code",
                    "model",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "step",
                    "stage",
                    "reason",
                    "source",
                    "compressed",
                    "context_tokens",
                    "context_limit_tokens",
                    "tool_calls_remaining",
                    "tokens_remaining",
                    "model_calls_remaining_including_current",
                    "steps_remaining_including_current",
                }
                await emit({k: v for k, v in event.items() if k in safe_keys})

        event_token = delegation_event_sink.set(record_event)
        try:
            await self.store.record({"run_id": run_id, "status": "running", "events": []}, context)
            detect_prompt_injection(request.message)
            if request.confirm_action_id:
                if emit:
                    await emit(
                        {
                            "event": "tool_started",
                            "run_id": run_id,
                            "parent_run_id": None,
                            "agent": "main",
                            "trace_id": trace_id,
                            "session_id": session_id,
                            "action_id": request.confirm_action_id,
                        }
                    )
                result = await self._confirm(request, context)
                result.update(
                    run_id=run_id,
                    events=[
                        {
                            "event": "confirmation",
                            "run_id": run_id,
                            "parent_run_id": None,
                            "agent": "main",
                            "trace_id": trace_id,
                            "session_id": session_id,
                            "action_id": request.confirm_action_id,
                            "status": result["status"],
                        }
                    ],
                )
                if emit:
                    await emit(
                        {
                            "event": "tool_completed",
                            "run_id": run_id,
                            "parent_run_id": None,
                            "agent": "main",
                            "trace_id": trace_id,
                            "session_id": session_id,
                            "status": result["status"],
                        }
                    )
            elif facts.get("_case_cancelled"):
                await self.store.cancel_pending(context)
                result = {
                    "run_id": run_id,
                    "status": "completed",
                    "events": [],
                    "observations": [],
                    "answer": "已停止本次任务，尚未确认的方案已撤销。已执行的操作不会因此撤回。",
                }
            else:
                async with asyncio.timeout(self._settings.agent_deadline_seconds):
                    result = await self.runner.run(
                        request.message,
                        context,
                        self.main_spec,
                        history=history,
                        summary=summary,
                        facts=cases.model_facts(facts),
                        initial_observations=confirmed_item_observations(
                            facts, request.message, context.user_id
                        ),
                        emit=record_event,
                        run_id=run_id,
                    )
            if not request.confirm_action_id and not result.get("_output_validated"):
                result = await self.validator.validate(
                    request.message, result, allow_correction=False
                )
            if run_events:
                # Persist preflight and child-agent progress with the main run
                # record as well as the specialists' individual run records.
                result["events"] = list(run_events)
            await self.store.record(result, context)
            for observation in result.get("observations", []):
                if observation.get("source") == "confirmed_session_fact":
                    continue
                args = observation.get("arguments", {})
                if "order_id" in args:
                    facts["last_order"] = {
                        "order_id": args["order_id"],
                        "source": observation["name"],
                        "source_run_id": observation.get("child_run_id")
                        or observation["output"].get("child_run_id")
                        or result.get("run_id"),
                        "source_trace_id": trace_id,
                    }
                output = observation["output"].get("output", observation["output"])
                if observation["output"].get("success", True):
                    evidence_refs = facts.get("evidence_refs")
                    if not isinstance(evidence_refs, list):
                        evidence_refs = []
                        facts["evidence_refs"] = evidence_refs
                    for chunk in output.get("chunks", []):
                        ref = chunk.get("chunk_id")
                        if not ref:
                            continue
                        evidence_refs[:] = [
                            item for item in evidence_refs if item.get("chunk_id") != ref
                        ]
                        evidence_refs.append(
                            {
                                "chunk_id": ref,
                                "document_id": chunk.get("document_id"),
                                "title": chunk.get("title"),
                                "section": chunk.get("section"),
                                "policy_version": chunk.get("policy_version"),
                                "artifact_ref": observation.get("artifact_ref"),
                                "source_tool": observation["name"],
                                "source_run_id": observation.get("child_run_id")
                                or observation["output"].get("child_run_id")
                                or output.get("child_run_id")
                                or result.get("run_id"),
                                "source_trace_id": trace_id,
                            }
                        )
                    facts["evidence_refs"] = evidence_refs[-20:]
                if observation["name"] in (
                    "get_order",
                    "get_order_items",
                    "track_shipment",
                    "get_refund_status",
                ) and observation["output"].get("success"):
                    facts.setdefault("business", {})[
                        args.get("order_id", "") + ":" + observation["name"]
                    ] = {
                        "tool": observation["name"],
                        "result": output,
                        "source_run_id": observation.get("child_run_id")
                        or observation["output"].get("child_run_id")
                        or output.get("child_run_id")
                        or result.get("run_id"),
                        "source_trace_id": trace_id,
                    }
            facts = cases.update(facts, request.message, result)
            await self.store.save_facts(context, facts)
            pending = (
                ActionProposal.model_validate(result["pending_action"])
                if result.get("pending_action")
                else None
            )
            citations = {}
            degradations = []
            used_refs = cited_refs(result["answer"])
            for observation in result.get("observations", []):
                data = observation["output"].get("output", observation["output"])
                if (
                    observation["output"].get("success")
                    and data.get("validated")
                    and data.get("answer") == result["answer"]
                ):
                    used_refs.update(c["chunk_id"] for c in data.get("citations", []))
            for observation in result.get("observations", []):
                data = observation["output"].get("output", observation["output"])
                if data.get("generation_failure") or data.get("degraded"):
                    degradations.append(
                        {
                            "tool": observation["name"],
                            "generation_failure": data.get("generation_failure"),
                            "validation_failure": data.get("failure")
                            if data.get("degraded")
                            else None,
                        }
                    )
                for c in data.get("chunks", []):
                    if c["chunk_id"] not in used_refs:
                        continue
                    citations[c["chunk_id"]] = Citation(
                        document_id=c["document_id"],
                        chunk_id=c["chunk_id"],
                        title=c["title"],
                        section=c.get("section"),
                    )
            role_usage = {}
            for call in ledger.calls:
                if call["kind"] not in {"model", "model_reservation"}:
                    continue
                role = call["role"]
                usage = role_usage.setdefault(
                    role,
                    {
                        "model_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                )
                if call["kind"] == "model_reservation":
                    usage["model_calls"] += 1
                else:
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        usage[field] += call.get(field, 0) or 0
            attributed_calls = sum(item["model_calls"] for item in role_usage.values())
            unattributed_calls = max(0, budget.models - attributed_calls)
            attributed_tokens = sum(item["total_tokens"] for item in role_usage.values())
            unattributed_tokens = max(0, budget.usage.total_tokens - attributed_tokens)
            if unattributed_calls or unattributed_tokens:
                role_usage["validation_or_other"] = {
                    "model_calls": unattributed_calls,
                    "prompt_tokens": max(
                        0,
                        budget.usage.prompt_tokens
                        - sum(item["prompt_tokens"] for item in role_usage.values()),
                    ),
                    "completion_tokens": max(
                        0,
                        budget.usage.completion_tokens
                        - sum(item["completion_tokens"] for item in role_usage.values()),
                    ),
                    "total_tokens": unattributed_tokens,
                }
            delegation_results = [
                o for o in result.get("observations", []) if o.get("name") == "delegate_task"
            ]
            delegation_diagnostics = {
                "mode": self.delegation_mode.value,
                "required_experts": [],
                "completed_experts": sorted(
                    {
                        o["arguments"]["agent"]
                        for o in delegation_results
                        if o["output"].get("status") == "completed"
                    }
                ),
                "attempts": [
                    {
                        "expert": o["arguments"].get("agent"),
                        "status": o["output"].get("status"),
                        "child_run_id": o["output"].get("child_run_id"),
                    }
                    for o in delegation_results
                ],
                "budget_blocked": [],
                "delegate_calls": sum(
                    e.get("event") == "delegation_dispatched" for e in run_events
                ),
                "required_experts_complete": False,
            }
            response = ChatResponse(
                answer=result["answer"],
                run_id=result.get("run_id"),
                status=result.get("status", "completed"),
                error_code=(result.get("failure") or {}).get("code"),
                events=run_events or result.get("events", []),
                diagnostics={
                    "model_calls": budget.models,
                    "reserved_tokens_without_usage": budget.reserved_tokens,
                    "tool_calls": budget.tools,
                    "tools": [o["name"] for o in result.get("observations", [])],
                    "failure": result.get("failure"),
                    "validation": result.get("validation"),
                    "degradations": degradations,
                    "delegation": delegation_diagnostics,
                    "role_usage": role_usage,
                },
                session_id=session_id,
                trace_id=trace_id,
                citations=list(citations.values()),
                pending_action=pending,
                token_usage=budget.usage,
                latency=LatencyBreakdown(total_ms=(time.perf_counter() - started) * 1000),
            )
        except (BudgetExceeded, TimeoutError) as exc:
            failure = {
                "code": "RUN_BUDGET_EXHAUSTED",
                "reason": str(exc) or "deadline",
                "retryable": True,
            }
            run_events.append(
                {
                    "event": "stopped",
                    "run_id": run_id,
                    "parent_run_id": None,
                    "agent": "main",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    **failure,
                }
            )
            response = ChatResponse(
                answer="本轮执行达到预算或时间限制，请稍后重试。",
                status="budget_exhausted",
                error_code=failure["code"],
                run_id=run_id,
                events=run_events,
                diagnostics={"failure": failure},
                session_id=session_id,
                trace_id=trace_id,
                token_usage=budget.usage,
            )
            if run_id:
                await self.store.record(
                    {"run_id": run_id, "status": response.status, "events": run_events}, context
                )
        except LLMProviderError as exc:
            failure = {
                "code": exc.error_code.value,
                "reason": exc.message,
                "retryable": exc.retryable,
            }
            run_events.append(
                {
                    "event": "error",
                    "run_id": run_id,
                    "parent_run_id": None,
                    "agent": "main",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    **failure,
                }
            )
            response = ChatResponse(
                answer="模型暂不可用，本轮未能完成结果校验，请稍后重试。",
                status="failed",
                error_code=exc.error_code.value,
                run_id=run_id,
                session_id=session_id,
                trace_id=trace_id,
                token_usage=budget.usage,
                diagnostics={"failure": failure},
                events=run_events,
            )
            if run_id:
                await self.store.record(
                    {"run_id": run_id, "status": response.status, "events": run_events}, context
                )
        except GuardrailError as exc:
            run_events.append(
                {
                    "event": "guardrail_refusal",
                    "run_id": run_id,
                    "parent_run_id": None,
                    "agent": "main",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "error_code": exc.error_code.value,
                }
            )
            response = ChatResponse(
                run_id=run_id,
                answer=exc.message,
                session_id=session_id,
                trace_id=trace_id,
                status="refused",
                refused=True,
                error_code=exc.error_code.value,
                events=run_events,
            )
            await self.store.record(
                {"run_id": run_id, "status": response.status, "events": run_events}, context
            )
        finally:
            delegation_event_sink.reset(event_token)
            validation_runtime_context.reset(validation_token)
            active_run_id.reset(active_root_token)
            current_budget.reset(token)
            current_ledger.reset(ledger_token)
        response.diagnostics["execution_ledger"] = ledger.summary()
        response.diagnostics["business_case"] = facts.get("active_case")
        response.latency.total_ms = (time.perf_counter() - started) * 1000
        response.diagnostics.setdefault("model_calls", budget.models)
        response.diagnostics.setdefault("tool_calls", budget.tools)
        response.diagnostics.setdefault("reserved_tokens_without_usage", budget.reserved_tokens)
        async with self._session_factory() as db:
            repo = SessionRepository(db)
            await repo.append_message(session_id, "user", request.message)
            await repo.append_message(session_id, "assistant", response.answer)
            # Deterministic running summary supplements, never replaces, structured facts.
            excerpt = "\n".join(m.content for m in history[-6:])
            await repo.save_summary(
                session_id,
                (summary + "\n" + excerpt + "\n" + request.message + "\n" + response.answer)[
                    -4000:
                ],
            )
            await db.commit()
        return response

    async def _verify_faithfulness(
        self,
        *,
        query,
        answer,
        observations,
        chunks,
        tool_facts,
        known_refs,
        tool_contracts,
    ):
        runtime = validation_runtime_context.get()
        if runtime is None:
            raise LLMProviderError("faithfulness request context is unavailable", retryable=False)
        context = runtime["context"]
        root_run_id = active_run_id.get() or runtime.get("root_run_id")
        if not root_run_id:
            raise LLMProviderError("faithfulness root run is unavailable", retryable=False)

        # Keep the callable name for existing integrations. Critic gets an index
        # of server-owned observations, never Main-generated evidence or a new
        # all-evidence artifact. Existing owner/session-scoped reads remain the
        # only route to the source content.
        evidence_index = []
        for observation in observations:
            name = observation.get("name")
            if not name or name == "ask_user":
                continue
            output = observation.get("output", {})
            data = output.get("output", output)
            passages = data.get("chunks", data.get("evidence", []))
            refs = {name, observation.get("call_id", "")}
            refs.update(p.get("chunk_id", "") for p in passages if isinstance(p, dict))
            ref = observation.get("artifact_ref") or output.get("artifact_ref")
            sources = [
                "/".join(str(p.get(k, "")) for k in ("title", "section")).strip("/")[:120]
                for p in passages[:3]
                if isinstance(p, dict)
            ]
            evidence_index.append(
                {
                    "refs": sorted(r for r in refs if r and r in known_refs),
                    "source": (name + (": " + "; ".join(sources) if any(sources) else ""))[:400],
                    "artifact_ref": ref,
                }
            )
        allowed_artifacts = frozenset(
            item["artifact_ref"] for item in evidence_index if item["artifact_ref"]
        )
        history = runtime.get("history", [])
        background = [{"role": m.role, "content": m.content[:600]} for m in history[-4:]]
        if not background:
            background = [
                {"role": "user", "content": message[:600]}
                for message in context.user_messages[-4:-1]
            ]
        review_input = {
            "original_user_question": query,
            "necessary_conversation_context": background,
            "candidate_answer": answer,
            "evidence_index": evidence_index,
        }
        spec = AgentSpec(
            "critic",
            frozenset({"read_artifact"}),
            prompt=CRITIC_AGENT_PROMPT,
            max_steps=3,  # one optional read, verdict, at most one schema retry
            artifact_refs=allowed_artifacts,
        )
        emit = delegation_event_sink.get()
        parent_token = None
        if active_run_id.get() is None:
            parent_token = active_run_id.set(root_run_id)
        try:
            child = await self.runner.run(
                "审核 critic_input 中的候选答复，按需读取一次现有材料，输出裁定 JSON。",
                context,
                spec,
                initial_context={"critic_input": review_input},
                emit=emit,
            )
            if child.get("status") != "completed":
                failure = child.get("failure") or {}
                if child.get("status") == "budget_exhausted":
                    raise BudgetExceeded(failure.get("reason", "critic_budget_exhausted"))
                if failure.get("code") == "LLM_OUTPUT_INVALID":
                    raise LLMOutputError(failure.get("reason", "critic verdict invalid"))
                raise LLMProviderError(
                    failure.get("reason", "critic agent did not complete"),
                    retryable=failure.get("retryable", True),
                )
            verdict = parse_model(child.get("answer", ""), FaithfulnessVerdict)
            reads = [o for o in child.get("observations", []) if o.get("name") == "read_artifact"]

            def complete_material(value):
                if isinstance(value, list):
                    return all(complete_material(item) for item in value)
                if not isinstance(value, dict):
                    return True
                if (
                    value.get("success") is False
                    or value.get("facts_complete") is False
                    or value.get("evidence_complete") is False
                    or value.get("truncated") is True
                    or ("chunk_id" in value and not value.get("content"))
                    or ("content_excerpt" in value and not value.get("content"))
                    or value.get("status") == "read_failed"
                ):
                    return False
                return all(complete_material(item) for item in value.values())

            evidence_checked = False
            if reads:
                read = reads[0]
                output = read.get("output", {})
                data = output.get("output", output)
                if not isinstance(data, dict):
                    data = {}
                material = {
                    k: v
                    for k, v in data.items()
                    if k
                    not in (
                        "artifact_ref",
                        "summary",
                        "notice",
                        "success",
                        "evidence_refs",
                        "facts_complete",
                        "evidence_complete",
                        "missing_fields",
                        "kind",
                    )
                    and not (k == "status" and v in ("complete", "completed"))
                    and v not in (None, "", [], {})
                }
                evidence_checked = (
                    len(reads) == 1
                    and read.get("arguments", {}).get("ref") in allowed_artifacts
                    and bool(material)
                    and complete_material(output)
                )
                if not evidence_checked:
                    verdict = FaithfulnessVerdict(
                        verdict="insufficient_evidence",
                        issues=["本次所需材料读取失败、不完整或缺少可核验内容，关键疑点尚未核实。"],
                        correction_guidance="保留不确定性，补问缺少的信息或使用现有失败路径；不得猜测事实。",
                    )
            if verdict.verdict == "fail":
                verdict = verdict.model_copy(update={"verdict": "revise"})
            if verdict.verdict == "pass" and (verdict.issues or verdict.correction_guidance):
                verdict = verdict.model_copy(update={"verdict": "revise"})
            verdict = verdict.model_copy(
                update={
                    "evidence_checked": evidence_checked,
                    # Unread known index entries cannot certify facts. Preserve
                    # unknown refs so the existing deterministic check rejects
                    # a fabricated source instead of silently accepting it.
                    "evidence_refs": verdict.evidence_refs
                    if evidence_checked
                    else [ref for ref in verdict.evidence_refs if ref not in known_refs],
                }
            )
            verdict_event = {
                "event": "faithfulness_verdict",
                "run_id": child.get("run_id"),
                "parent_run_id": root_run_id,
                "trace_id": context.trace_id,
                "session_id": context.session_id,
                "agent": "critic",
                "verdict": verdict.verdict,
                "review_type": "critic",
                "evidence_checked": verdict.evidence_checked,
                "facts_verified": False,
                "issues": verdict.issues,
                "evidence_refs": verdict.evidence_refs,
            }
            child.setdefault("events", []).append(verdict_event)
            if emit:
                await emit(verdict_event)
            if self.store and child.get("run_id"):
                await self.store.record(child, context, parent_run_id=root_run_id)
            return verdict
        except Exception as exc:
            # Preserve an explicit failed child trace even when its output does
            # not satisfy the verdict schema.
            if "child" in locals() and self.store and child.get("run_id"):
                if child.get("status") == "completed":
                    child["status"] = "failed"
                    child["failure"] = {
                        "code": "FAITHFULNESS_VERDICT_INVALID",
                        "reason": type(exc).__name__,
                        "retryable": False,
                    }
                error_event = {
                    "event": "faithfulness_error",
                    "run_id": child.get("run_id"),
                    "parent_run_id": root_run_id,
                    "trace_id": context.trace_id,
                    "session_id": context.session_id,
                    "agent": "critic",
                    "reason": getattr(exc, "message", str(exc)) or type(exc).__name__,
                }
                child.setdefault("events", []).append(error_event)
                if emit:
                    await emit(error_event)
                await self.store.record(child, context, parent_run_id=root_run_id)
            raise
        finally:
            if parent_token is not None:
                active_run_id.reset(parent_token)

    async def _confirm(self, request, context):
        return await self.executor.confirm(request.confirm_action_id, context, self.spec)

    async def stream(self, request: ChatRequest, *, trace_id: str):
        queue: asyncio.Queue = asyncio.Queue()

        async def work():
            try:
                response = await self.chat(request, trace_id=trace_id, emit=queue.put)
                await queue.put({"event": "answer", "data": response.model_dump(mode="json")})
            except Exception as exc:
                await queue.put({"event": "error", "error": type(exc).__name__})
            finally:
                await queue.put(None)

        yield {"event": "started", "trace_id": trace_id}
        task = asyncio.create_task(work())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await task
            yield {"event": "done", "trace_id": trace_id}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
