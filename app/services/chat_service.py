"""Conversation persistence, deterministic confirmation and harness streaming."""

from __future__ import annotations

import asyncio
import time

from app.config.settings import Settings
from app.core.exceptions import GuardrailError, InvalidRequestError, RateLimitError
from app.core.ids import new_id
from app.guardrails.injection import detect_prompt_injection
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.harness.delegation import DelegationService
from app.harness.executor import ToolExecutor
from app.harness.runner import AgentRunner, AgentSpec
from app.harness.validation import AnswerValidator
from app.providers.fake import OfflineLLMProvider
from app.repositories.runtime import RuntimeStore
from app.repositories.session_repository import SessionRepository
from app.schemas.chat import ActionProposal, ChatRequest, ChatResponse
from app.schemas.common import LatencyBreakdown
from app.schemas.retrieval import Citation
from app.services.knowledge import KnowledgeService
from app.tools.base import ToolContext
from app.tools.factory import build_tool_registry
from app.tools.knowledge_service import EvidenceTool, PolicyAnswerTool


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
            else BudgetedProvider(llm, settings.agent_context_tokens)
        )
        self.knowledge = KnowledgeService(retriever, reranker, self.provider, settings)
        self.registry.register(EvidenceTool(self.knowledge))
        self.registry.register(PolicyAnswerTool(self.knowledge))
        self.executor = ToolExecutor(self.registry, session_factory, settings.agent_tool_timeout)
        self.runner = AgentRunner(self.provider, self.executor, settings)
        self.store = RuntimeStore(session_factory)
        self.executor.artifacts = self.store
        self.validator = AnswerValidator(self.provider)
        self.spec = AgentSpec(
            "main",
            frozenset([*self.registry.names(), "load_skill", "read_artifact"]),
            max_steps=settings.max_agent_steps,
        )
        if settings.agent_delegation_enabled:
            self.executor.delegate = DelegationService(self.runner, self.executor, self.store)
            self.spec = AgentSpec(
                "main", self.spec.tools | {"delegate_task"}, max_steps=settings.max_agent_steps
            )
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_actions: dict = {}

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
        context = ToolContext(user_id=request.user_id, session_id=session_id, trace_id=trace_id)
        async with self._session_factory() as db:
            repo = SessionRepository(db)
            row = await repo.get_or_create(session_id, request.user_id)
            summary = row.summary or ""
            history = await repo.recent_messages(session_id)
            await db.commit()
        budget = RunBudget(
            time.monotonic() + self._settings.agent_deadline_seconds,
            max_models=self._settings.agent_model_calls,
            max_tools=self._settings.max_tool_calls,
            max_tokens=self._settings.agent_total_tokens,
        )
        facts = await self.store.facts(context)
        token = current_budget.set(budget)
        try:
            detect_prompt_injection(request.message)
            if request.confirm_action_id:
                result = await self._confirm(request, context)
            else:
                async with asyncio.timeout(self._settings.agent_deadline_seconds):
                    result = await self.runner.run(
                        request.message,
                        context,
                        self.spec,
                        history=history,
                        summary=summary,
                        facts=facts,
                        emit=emit,
                    )
            if not request.confirm_action_id:
                result = await self.validator.validate(request.message, result)
                await self.store.record(result, context)
            for observation in result.get("observations", []):
                args = observation.get("arguments", {})
                if "order_id" in args:
                    facts["last_order"] = {
                        "order_id": args["order_id"],
                        "source": observation["name"],
                    }
            await self.store.save_facts(context, facts)
            pending = (
                ActionProposal.model_validate(result["pending_action"])
                if result.get("pending_action")
                else None
            )
            if pending:
                self._pending_actions[pending.action_id] = (context, pending, time.time() + 900)
            citations = {}
            for observation in result.get("observations", []):
                data = observation["output"].get("output", observation["output"])
                for c in data.get("chunks", []):
                    citations[c["chunk_id"]] = Citation(
                        document_id=c["document_id"],
                        chunk_id=c["chunk_id"],
                        title=c["title"],
                        section=c.get("section"),
                    )
            response = ChatResponse(
                answer=result["answer"],
                session_id=session_id,
                trace_id=trace_id,
                citations=list(citations.values()),
                pending_action=pending,
                token_usage=budget.usage,
                latency=LatencyBreakdown(total_ms=(time.perf_counter() - started) * 1000),
            )
        except (BudgetExceeded, TimeoutError):
            response = ChatResponse(
                answer="本轮执行达到预算或时间限制，请稍后重试。",
                session_id=session_id,
                trace_id=trace_id,
                token_usage=budget.usage,
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
            current_budget.reset(token)
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

    async def _confirm(self, request, context):
        pending = self._pending_actions.get(request.confirm_action_id)
        if (
            not pending
            or pending[0].user_id != context.user_id
            or pending[0].session_id != context.session_id
            or pending[2] < time.time()
        ):
            raise InvalidRequestError("待确认操作不存在或已过期，请重新发起。")
        action = pending[1]
        context.confirmed_action_id = action.action_id
        context.idempotency_key = action.action_id
        result = await self.executor.execute(
            action.tool_name, action.arguments, context, self.spec, confirmed=True
        )
        self._pending_actions.pop(action.action_id, None)
        label = "已取消" if action.tool_name == "cancel_order" else "申请已提交，尚不代表退款完成"
        return {
            "answer": f"订单 {action.arguments.get('order_id','')} {label}。",
            "observations": [{"name": action.tool_name, "output": result}],
        }

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
