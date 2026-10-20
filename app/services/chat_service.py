"""Conversation persistence, deterministic confirmation and harness streaming."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar

from app.config.settings import Settings
from app.core.exceptions import (
    GuardrailError,
    InvalidRequestError,
    LLMProviderError,
    RateLimitError,
)
from app.core.ids import new_id
from app.guardrails.injection import detect_prompt_injection
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.delegation import (
    DelegationService,
    delegation_event_sink,
    delegation_include_observations,
    delegation_observations,
)
from app.harness.delegation_policy import DelegationMode, DelegationPolicy
from app.harness.evidence import confirmed_item_observations
from app.harness.executor import ToolExecutor
from app.harness.runner import AgentRunner, AgentSpec, active_run_id
from app.harness.validation import AnswerValidator, FaithfulnessVerdict
from app.prompts.faithfulness import FAITHFULNESS_AGENT_PROMPT
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
        self.runner = AgentRunner(self.provider, self.executor, settings)
        self.store = RuntimeStore(session_factory)
        self.executor.artifacts = self.store
        self.spec = AgentSpec(
            "main",
            frozenset([*self.registry.names(), "load_skill", "read_artifact", "ask_user"]),
            max_steps=settings.max_agent_steps,
        )
        self.delegation_mode = (
            DelegationMode(settings.agent_delegation_policy)
            if settings.agent_delegation_enabled
            else DelegationMode.OFF
        )
        self.delegation_policy = DelegationPolicy(self.delegation_mode)
        self.delegation_enabled = self.delegation_mode is not DelegationMode.OFF
        if self.delegation_enabled:
            self.executor.delegate = DelegationService(self.runner, self.executor, self.store)
            self.spec = AgentSpec(
                "main", self.spec.tools | {"delegate_task"}, max_steps=settings.max_agent_steps
            )
        self.validator = AnswerValidator(
            self.provider,
            tool_contracts=self.executor.schemas(self.spec),
            verifier=self._verify_faithfulness,
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
        token = current_budget.set(budget)
        validation_state = {"context": context, "facts": facts, "root_run_id": None}
        validation_token = validation_runtime_context.set(validation_state)
        run_id = None
        run_events = []
        result = {}
        preflight_observations = confirmed_item_observations(
            facts, request.message, context.user_id
        )
        delegation_context = {}
        delegation_attempts = []

        async def record_event(event):
            nonlocal run_id
            if event.get("parent_run_id") is None:
                run_id = event.get("run_id", run_id)
                if run_id:
                    validation_state["root_run_id"] = run_id
            run_events.append(event)
            if emit:
                await emit(event)

        event_token = delegation_event_sink.set(record_event)
        try:
            detect_prompt_injection(request.message)
            if request.confirm_action_id:
                run_id = new_id("run_")
                if emit:
                    await emit(
                        {
                            "event": "tool_started",
                            "run_id": run_id,
                            "action_id": request.confirm_action_id,
                        }
                    )
                result = await self._confirm(request, context)
                result.update(
                    run_id=run_id,
                    events=[
                        {
                            "event": "confirmation",
                            "action_id": request.confirm_action_id,
                            "status": result["status"],
                        }
                    ],
                )
                if emit:
                    await emit(
                        {"event": "tool_completed", "run_id": run_id, "status": result["status"]}
                    )
            else:
                if self.delegation_mode is DelegationMode.REQUIRED_WHEN_ELIGIBLE:
                    decision = self.delegation_policy.decide(
                        request.message, history=history, confirmed_facts=facts
                    )
                    await record_event(
                        {
                            "event": "delegation_plan",
                            "mode": decision.mode.value,
                            "required_experts": list(decision.experts),
                            "reasons": list(decision.reasons),
                        }
                    )
                    delegation_context = {
                        "mode": decision.mode.value,
                        "required_experts": list(decision.experts),
                        "reasons": list(decision.reasons),
                        "experts": {},
                    }
                    if decision.experts:
                        budget.protected_tokens = min(
                            max(0, self._settings.agent_delegation_main_reserve_tokens),
                            max(0, budget.max_tokens - budget.usage.total_tokens),
                        )
                        budget.protected_models = min(
                            max(0, self._settings.agent_delegation_main_reserve_model_calls),
                            budget.max_models,
                        )
                        budget.protected_tools = min(
                            max(0, self._settings.agent_delegation_main_reserve_tool_calls),
                            budget.max_tools,
                        )
                    try:
                        for index, expert in enumerate(decision.experts):
                            attempt = {
                                "expert": expert,
                                "status": "started",
                                "missing_fields": [],
                                "evidence_refs": [],
                                "child_run_id": None,
                            }
                            await record_event(
                                {
                                    "event": "delegation_started",
                                    "source": "c_preflight",
                                    "expert": expert,
                                    "tool": "delegate_task",
                                }
                            )
                            try:
                                include_token = delegation_include_observations.set(True)
                                evidence_token = delegation_observations.set(
                                    tuple(preflight_observations)
                                )
                                try:
                                    with budget.delegation_share(
                                        len(decision.experts) - index
                                    ) as allocation:
                                        await record_event(
                                            {
                                                "event": "delegation_allocation",
                                                "expert": expert,
                                                "allocation": allocation,
                                                "source": "c_preflight",
                                            }
                                        )
                                        output = await self.executor.execute(
                                            "delegate_task",
                                            {
                                                "agent": expert,
                                                "goal": DelegationPolicy.goal_for_expert(
                                                    expert, request.message
                                                ),
                                                "context": DelegationPolicy.context_for_expert(
                                                    request.message,
                                                    history,
                                                    {
                                                        **facts,
                                                        "verified_observations": preflight_observations,
                                                    },
                                                ),
                                            },
                                            context,
                                            self.spec,
                                        )
                                finally:
                                    delegation_observations.reset(evidence_token)
                                    delegation_include_observations.reset(include_token)
                                attempt.update(
                                    status=output.get("status", "incomplete"),
                                    missing_fields=output.get("missing_fields", []),
                                    evidence_refs=output.get("evidence_refs", []),
                                    child_run_id=output.get("child_run_id"),
                                    findings=output.get("findings", []),
                                    missing_information=output.get("missing_information", []),
                                    policy_result_status=output.get("policy_result_status"),
                                )
                                attempt.update(
                                    fact_refs=[
                                        {
                                            "tool": fact["tool"],
                                            "arguments": fact.get("arguments", {}),
                                        }
                                        for fact in output.get("facts", [])
                                    ],
                                    summary=output.get("summary", "")[:600],
                                )
                                for observation in output.get("observations", []):
                                    if observation in preflight_observations:
                                        continue
                                    preflight_observations.append(
                                        {
                                            **observation,
                                            "delegated_by": expert,
                                            "child_run_id": output.get("child_run_id"),
                                        }
                                    )
                            except BudgetExceeded as exc:
                                attempt.update(
                                    status="budget_blocked",
                                    missing_fields=["delegation_budget"],
                                    budget_scope=exc.details.get("budget_scope", "run_budget"),
                                )
                            except Exception as exc:
                                # Fail closed on the specialist attempt. The main
                                # agent still receives its original tools and can
                                # answer from available evidence or ask a question.
                                attempt.update(
                                    status="failed",
                                    missing_fields=["expert_execution"],
                                    failure=type(exc).__name__,
                                )
                            delegation_attempts.append(attempt)
                            delegation_context["experts"][expert] = {
                                key: attempt[key]
                                for key in (
                                    "status",
                                    "missing_fields",
                                    "evidence_refs",
                                    "child_run_id",
                                    "fact_refs",
                                    "summary",
                                    "findings",
                                    "missing_information",
                                    "policy_result_status",
                                )
                                if key in attempt
                            }
                            await record_event(
                                {
                                    "event": "delegation_result",
                                    "source": "c_preflight",
                                    "expert": expert,
                                    "status": attempt["status"],
                                    "child_run_id": attempt.get("child_run_id"),
                                    "missing_fields": attempt["missing_fields"],
                                    "budget_scope": attempt.get("budget_scope"),
                                }
                            )
                    finally:
                        # Child work cannot consume the protected main-agent
                        # continuation budget. The shared task cap is unchanged.
                        budget.protected_tokens = 0
                        budget.protected_models = 0
                        budget.protected_tools = 0
                async with asyncio.timeout(self._settings.agent_deadline_seconds):
                    result = await self.runner.run(
                        request.message,
                        context,
                        self.spec,
                        history=history,
                        summary=summary,
                        facts=facts,
                        initial_observations=preflight_observations,
                        initial_context=delegation_context,
                        emit=record_event,
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
                    }
                output = observation["output"].get("output", observation["output"])
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
                    }
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
            for event in run_events:
                if event.get("event") not in ("model_started", "model_completed"):
                    continue
                role = event.get("agent", "unknown")
                usage = role_usage.setdefault(
                    role,
                    {
                        "model_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                )
                if event.get("event") == "model_started":
                    usage["model_calls"] += 1
                else:
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        usage[field] += event.get(field, 0) or 0
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
            in_run_child_ids = {
                (o.get("output") or {}).get("child_run_id")
                for o in result.get("observations", [])
                if o.get("name") == "delegate_task" and (o.get("output") or {}).get("child_run_id")
            }
            successful_preflight_ids = {
                item.get("child_run_id") for item in delegation_attempts if item.get("child_run_id")
            }
            delegation_diagnostics = {
                "mode": self.delegation_mode.value,
                "required_experts": list(delegation_context.get("required_experts", [])),
                "completed_experts": [
                    item["expert"]
                    for item in delegation_attempts
                    if item.get("status") == "completed"
                ],
                "attempts": delegation_attempts,
                "budget_blocked": [
                    item["expert"]
                    for item in delegation_attempts
                    if item.get("status") == "budget_blocked"
                ],
                "delegate_calls": max(
                    sum(event.get("event") == "delegation_dispatched" for event in run_events),
                    len(successful_preflight_ids | in_run_child_ids),
                ),
                "required_experts_complete": bool(delegation_context.get("required_experts"))
                and all(item.get("status") == "completed" for item in delegation_attempts)
                and len(delegation_attempts) == len(delegation_context.get("required_experts", [])),
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
            run_events.append({"event": "stopped", "run_id": run_id, **failure})
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
            run_events.append({"event": "error", "run_id": run_id, **failure})
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
            response = ChatResponse(
                answer=exc.message,
                session_id=session_id,
                trace_id=trace_id,
                refused=True,
                error_code=exc.error_code.value,
            )
        finally:
            delegation_event_sink.reset(event_token)
            validation_runtime_context.reset(validation_token)
            current_budget.reset(token)
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

        observed_results = []
        for observation in observations:
            output = observation.get("output", {})
            data = output.get("output", output)
            observed_results.append(
                {
                    "name": observation.get("name"),
                    "arguments": observation.get("arguments", {}),
                    "call_id": observation.get("call_id"),
                    "success": output.get("success", True),
                    "artifact_ref": observation.get("artifact_ref") or output.get("artifact_ref"),
                    "result": {
                        key: value
                        for key, value in data.items()
                        if key not in ("chunks", "answer", "summary", "validation")
                    },
                }
            )
        evidence_input = {
            "original_user_question": query,
            "candidate_answer": answer,
            "raw_observations": observed_results,
            "business_tool_results": tool_facts,
            "policy_evidence": [chunk.model_dump(mode="json") for chunk in chunks],
            "trusted_session_facts": runtime.get("facts", {}),
            "available_tools": tool_contracts,
            "allowed_evidence_refs": sorted(ref for ref in known_refs if ref),
        }
        spec = AgentSpec(
            "faithfulness",
            frozenset({"read_artifact"}),
            prompt=FAITHFULNESS_AGENT_PROMPT,
            max_steps=3,
        )
        emit = delegation_event_sink.get()
        parent_token = None
        if active_run_id.get() is None:
            parent_token = active_run_id.set(root_run_id)
        try:
            child = await self.runner.run(
                "只审核 faithfulness_input 中的候选答复，并输出规定的 JSON 裁定。",
                context,
                spec,
                initial_context={"faithfulness_input": evidence_input},
                emit=emit,
            )
            if child.get("status") != "completed":
                failure = child.get("failure") or {}
                raise LLMProviderError(
                    failure.get("reason", "faithfulness agent did not complete"),
                    retryable=failure.get("retryable", True),
                )
            verdict = parse_model(child.get("answer", ""), FaithfulnessVerdict)
            child.setdefault("events", []).append(
                {
                    "event": "faithfulness_verdict",
                    "run_id": child.get("run_id"),
                    "parent_run_id": root_run_id,
                    "verdict": verdict.verdict,
                    "issues": verdict.issues,
                    "evidence_refs": verdict.evidence_refs,
                }
            )
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
                child.setdefault("events", []).append(
                    {
                        "event": "faithfulness_error",
                        "run_id": child.get("run_id"),
                        "parent_run_id": root_run_id,
                        "reason": type(exc).__name__,
                    }
                )
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
