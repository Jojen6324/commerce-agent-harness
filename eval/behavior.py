"""API task evaluation with source-family splits; no intent-to-tool mapping."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.config.settings import Settings
from app.main import create_app
from app.prompts.delegation import PROMPT_VERSION
from app.services.container import build_container
from eval.task_fixtures import install_fixture, matches, snapshot
from eval.task_grading import grade_turn
from eval.task_review import corpus_digest, response_digest
from eval.task_schema import DATA_DIR, AgentTask, load_tasks


def cases():
    return [task.model_dump() for task in load_tasks()]


def corpus_summary(tasks):
    return {
        "original_count": sum(t.variant == "original" for t in tasks),
        "derived_count": sum(t.variant == "derived" for t in tasks),
        "conversation_count": len(tasks),
        "multi_turn_conversation_count": sum(len(t.turns) > 1 for t in tasks),
        "turn_count": sum(len(t.turns) for t in tasks),
        "source_family_count": len({t.source_id for t in tasks}),
        "split_group_count": len({t.split_group for t in tasks}),
        "split_counts": dict(Counter(t.split for t in tasks)),
        "scenario_counts": dict(Counter(t.scenario for t in tasks)),
    }


def summarize(results, corpus=None):
    grouped = defaultdict(list)
    if corpus:
        for task in corpus:
            grouped[task.scenario]
    for row in results:
        grouped[row["scenario"]].append(row)

    def counts(rows):
        return {
            "n": len(rows),
            "task_status": {
                s: sum(r["task_status"] == s for r in rows)
                for s in ("passed", "failed", "review_required", "error")
            },
            "safety_violation_count": sum(
                len(t.get("safety_failures", [])) for r in rows for t in r["turns"]
            ),
            "deterministic_passed": sum(r["deterministic_status"] == "passed" for r in rows),
            "deterministic_failed": sum(r["deterministic_status"] == "failed" for r in rows),
        }

    return {
        "overall": counts(results),
        "by_scenario": {
            k: {
                **counts(v),
                "corpus_n": sum(t.scenario == k for t in corpus) if corpus else len(v),
                "unrun": sum(t.scenario == k for t in corpus) - len(v) if corpus else 0,
            }
            for k, v in sorted(grouped.items())
        },
        "by_split": {
            s: counts([r for r in results if r["split"] == s]) for s in ("development", "holdout")
        },
        "by_source_family": {
            sid: {
                **counts([r for r in results if r["source_id"] == sid]),
                "member_ids": [r["id"] for r in results if r["source_id"] == sid],
                "complete": sum(t.source_id == sid for t in corpus)
                == sum(r["source_id"] == sid for r in results)
                if corpus
                else None,
            }
            for sid in sorted({r["source_id"] for r in results})
        },
        "by_variant": {
            v: counts([r for r in results if r["variant"] == v]) for v in ("original", "derived")
        },
    }


async def evaluate(
    *,
    live=False,
    limit=None,
    split="development",
    task_ids=None,
    container_factory=build_container,
    dataset_path=None,
    delegation_enabled=None,
    delegation_policy=None,
    arm_name=None,
    monitor=False,
    provider="agnes",
):
    # Keep the locked 116-task historical corpus unchanged; opt in to new benchmarks.
    if dataset_path is None:
        all_tasks = load_tasks()
    else:
        all_tasks = [
            AgentTask.model_validate_json(line)
            for line in Path(dataset_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        valid_fixtures = set(json.loads((DATA_DIR / "agent_fixtures.json").read_text())["profiles"])
        if not all_tasks or len({t.id for t in all_tasks}) != len(all_tasks):
            raise ValueError("empty or duplicate benchmark task ids")
        if any(not set(t.fixtures) <= valid_fixtures for t in all_tasks):
            raise ValueError("benchmark refers to unknown fixture")
    if split not in ("development", "holdout", "all"):
        raise ValueError("invalid split")
    selected = [
        t
        for t in all_tasks
        if (split == "all" or t.split == split) and (not task_ids or t.id in task_ids)
    ]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    if not selected:
        raise ValueError("no tasks selected")
    if provider not in ("agnes", "deepseek"):
        raise ValueError("provider must be agnes or deepseek")
    results = []
    monitor_events = []
    arm = arm_name or (
        "multi" if delegation_enabled else "single" if delegation_enabled is not None else "default"
    )
    task_monitor_state = {}

    def log_progress(event, *, task_id=None, turn=None, **details):
        if not monitor:
            return
        record = {
            "at": datetime.now(UTC).isoformat(),
            "arm": arm,
            "delegation_enabled": delegation_enabled,
            "delegation_policy": delegation_policy,
            "task_id": task_id,
            "turn": turn,
            "event": event,
            **details,
        }
        state = task_monitor_state.get(task_id)
        if state is not None:
            if event == "delegation" and details.get("tool") == "delegate_task":
                call_id = details.get("tool_call_id")
                if call_id:
                    state["delegation_call_ids"].add(call_id)
            if event == "agent_started" and details.get("agent") in (
                "orders",
                "policy",
                "knowledge",
            ):
                state["specialist_agents"].add(details["agent"])
            if event == "delegation_plan":
                state["required_experts"].update(details.get("required_experts", []))
            if event == "delegation_dispatched":
                state["delegate_dispatch_count"] += 1
            if event == "delegation_result":
                expert = details.get("expert")
                if expert and details.get("status") == "completed":
                    state["completed_experts"].add(expert)
                if expert and details.get("status") == "budget_blocked":
                    state["budget_blocked_experts"].add(expert)
                if details.get("child_run_id"):
                    state["child_run_ids"].add(details["child_run_id"])
        monitor_events.append(record)
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)

    async def request_chat(client, payload, *, task_id, turn):
        request_started = time.perf_counter()
        if not monitor:
            response = await client.post("/api/v1/chat", json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {"error": "non_json_api_response"}
            return (
                response.status_code,
                body,
                round((time.perf_counter() - request_started) * 1000, 2),
            )

        log_progress("turn_started", task_id=task_id, turn=turn)
        status_code = 0
        final_body = None
        stream_error_type = None
        data_lines = []
        async with client.stream("POST", "/api/v1/chat/stream", json=payload) as response:
            status_code = response.status_code
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    raw = "\n".join(data_lines)
                    data_lines.clear()
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        log_progress("stream_parse_error", task_id=task_id, turn=turn)
                        continue
                    name = (
                        item.get("event", "stream_event")
                        if isinstance(item, dict)
                        else "stream_event"
                    )
                    if name == "answer" and isinstance(item.get("data"), dict):
                        final_body = item["data"]
                        continue
                    if name == "error" and isinstance(item.get("error"), str):
                        stream_error_type = item["error"]
                    if name not in ("started", "done"):
                        safe_fields = (
                            "agent",
                            "parent_run_id",
                            "run_id",
                            "tool",
                            "tool_call_id",
                            "elapsed_ms",
                            "reason",
                            "attempt",
                            "reused",
                            "compressed",
                            "context_tokens",
                            "context_limit_tokens",
                            "reserve_tokens",
                            "message_count",
                            "tool_count",
                            "stage",
                            "step",
                            "model",
                            "finish_reason",
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                            "reserved_tokens_without_usage",
                            "max_total_tokens",
                            "requested_tokens",
                            "projected_tokens",
                            "model_calls",
                            "model_call_limit",
                            "tool_calls",
                            "tool_call_limit",
                            "budget_scope",
                            "error_code",
                            "status",
                            "error",
                            "required_experts",
                            "completed_experts",
                            "expert",
                            "source",
                            "child_run_id",
                            "missing_fields",
                            "delegate_calls",
                            "required_experts_complete",
                            "budget_blocked",
                            "reasons",
                            "allocation",
                            "tool_calls_remaining",
                            "delegation_calls_remaining",
                            "model_calls_remaining_including_current",
                            "tokens_remaining",
                            "steps_remaining_including_current",
                        )
                        details = {key: item[key] for key in safe_fields if key in item}
                        if name == "error" and "error" in details:
                            details["stream_error_type"] = details.pop("error")
                        validation = item.get("validation")
                        if isinstance(validation, dict):
                            details["validation_status"] = validation.get("status")
                            details["validation_attempts"] = len(validation.get("attempts", []))
                        log_progress(name, task_id=task_id, turn=turn, **details)
            if data_lines:
                try:
                    item = json.loads("\n".join(data_lines))
                    if item.get("event") == "answer" and isinstance(item.get("data"), dict):
                        final_body = item["data"]
                    elif item.get("event") == "error" and isinstance(item.get("error"), str):
                        stream_error_type = item["error"]
                except ValueError:
                    pass
            if final_body is None and status_code >= 400:
                try:
                    final_body = json.loads((await response.aread()).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    final_body = {"error": "non_json_api_response"}
        final_body = final_body or {"error": "stream_ended_without_answer"}
        if stream_error_type:
            final_body.setdefault("stream_error_type", stream_error_type)
        diagnostics = final_body.get("diagnostics", {})
        usage = final_body.get("token_usage", {})
        elapsed_ms = round((time.perf_counter() - request_started) * 1000, 2)
        log_progress(
            "turn_finished",
            task_id=task_id,
            turn=turn,
            http_status=status_code,
            status=final_body.get("status"),
            error_code=final_body.get("error_code"),
            stream_error_type=final_body.get("stream_error_type"),
            model_calls=diagnostics.get("model_calls"),
            tool_calls=diagnostics.get("tool_calls"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            wall_elapsed_ms=elapsed_ms,
            reserved_tokens_without_usage=diagnostics.get("reserved_tokens_without_usage"),
            failure=diagnostics.get("failure"),
        )
        return status_code, final_body, elapsed_ms

    with tempfile.TemporaryDirectory(prefix="agent-tasks-") as directory:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{directory}/tasks.db",
            app_env="development" if live else "testing",
            redis_url="",
            qdrant_url="",
            mcp_remote_url="",
            rate_limit_per_minute=max(60, sum(len(t.turns) for t in selected) * 2),
        )
        if not live:
            settings = settings.model_copy(
                update={"agnes": SecretStr(""), "deepseek_api_key": SecretStr("")}
            )
        elif provider == "agnes" and not settings.agnes.get_secret_value():
            raise RuntimeError("AGNES binding required for live evaluation")
        elif provider == "deepseek":
            if not settings.deepseek_api_key.get_secret_value():
                raise RuntimeError("DeepSeek binding required for live evaluation")
            # Explicit selection prevents an AGNES binding from taking priority.
            settings = settings.model_copy(
                update={"agnes": SecretStr(""), "deepseek_thinking": "disabled"}
            )
        if delegation_enabled is not None:
            settings = settings.model_copy(
                update={
                    "agent_delegation_enabled": delegation_enabled,
                    "agent_delegation_policy": delegation_policy
                    or ("auto" if delegation_enabled else "off"),
                }
            )
        c = await container_factory(settings)
        app = create_app(settings)
        app.state.container = c
        # Audit every dispatch while retaining all production executor checks.
        calls = []
        active_dispatch: ContextVar[dict | None] = ContextVar("evaluation_dispatch", default=None)

        def observe_run(bound_run):
            async def run(arguments, context):
                entry = active_dispatch.get()
                if entry is not None:
                    entry["executed"] = True
                return await bound_run(arguments, context)

            return run

        for tool in c.chat_service.registry.all():
            tool.run = observe_run(tool.run)

        dispatch = c.chat_service.executor.execute

        async def audited(name, arguments, context, spec, **kwargs):
            entry = {
                "name": name,
                "arguments": copy.deepcopy(arguments),
                "user_id": context.user_id,
                "session_id": context.session_id,
                "confirmed": bool(kwargs.get("confirmed")),
                "executed": False,
                "success": False,
            }
            calls.append(entry)
            audit_token = active_dispatch.set(entry)
            try:
                output = await dispatch(name, arguments, context, spec, **kwargs)
                entry["success"] = bool(
                    output.get(
                        "success", output.get("status") in ("completed", "awaiting_confirmation")
                    )
                )
                entry["result"] = copy.deepcopy(output)
                return output
            except Exception as exc:
                entry["error"] = type(exc).__name__
                raise
            finally:
                active_dispatch.reset(audit_token)

        c.chat_service.executor.execute = audited
        index = {r.chunk.chunk_id: r.chunk.model_dump(mode="json") for r in c.vector_store._records}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://task-eval",
            ) as client:
                for task in selected:
                    task_monitor_state[task.id] = {
                        "delegation_call_ids": set(),
                        "specialist_agents": set(),
                        "required_experts": set(),
                        "completed_experts": set(),
                        "budget_blocked_experts": set(),
                        "child_run_ids": set(),
                        "delegate_calls": 0,
                        "delegate_dispatch_count": 0,
                    }
                    log_progress(
                        "task_started",
                        task_id=task.id,
                        scenario=task.scenario,
                        delegation_expected=None,
                    )
                    await install_fixture(c.session_factory, task.fixtures)
                    calls.clear()
                    session_id = None
                    responses = []
                    turns = []
                    session_calls = []
                    known_order_ids = set()
                    for n, turn in enumerate(task.turns, 1):
                        log_progress("turn_request", task_id=task.id, turn=n)
                        payload = {"message": turn.message, "user_id": turn.user_id}
                        if session_id:
                            payload["session_id"] = session_id
                        if turn.confirm_from_turn is not None:
                            earlier = responses[turn.confirm_from_turn - 1]
                            proposal = earlier.get("pending_action") or {}
                            expectations = task.turns[turn.confirm_from_turn - 1].checks
                            valid = any(
                                check.kind == "proposal"
                                and proposal.get("tool_name") == check.value["tool_name"]
                                and matches(proposal.get("arguments", {}), check.value["arguments"])
                                for check in expectations
                            )
                            if not proposal.get("action_id") or not valid:
                                turns.append(
                                    {
                                        "turn": n,
                                        "status": "blocked",
                                        "failures": ["no valid expected proposal to confirm"],
                                        "review_flags": [],
                                    }
                                )
                                responses.append({})
                                continue
                            payload["confirm_action_id"] = proposal["action_id"]
                        known_order_ids.update(
                            re.findall(r"(?<![A-Za-z0-9_])ORD[A-Za-z0-9_-]+", turn.message, re.I)
                        )
                        before = await snapshot(c.session_factory)
                        start = len(calls)
                        status_code, body, elapsed_ms = await request_chat(
                            client, payload, task_id=task.id, turn=n
                        )
                        responses.append(body)
                        if status_code == 200:
                            session_id = body.get("session_id", session_id)
                        after = await snapshot(c.session_factory)
                        delegation_info = (body.get("diagnostics") or {}).get("delegation") or {}
                        state = task_monitor_state[task.id]
                        state["required_experts"].update(
                            delegation_info.get("required_experts", [])
                        )
                        state["completed_experts"].update(
                            delegation_info.get("completed_experts", [])
                        )
                        state["budget_blocked_experts"].update(
                            delegation_info.get("budget_blocked", [])
                        )
                        state["delegate_calls"] += delegation_info.get("delegate_calls", 0) or 0
                        if monitor and delegation_info:
                            log_progress(
                                "delegation_evaluation",
                                task_id=task.id,
                                turn=n,
                                required_experts=delegation_info.get("required_experts", []),
                                completed_experts=delegation_info.get("completed_experts", []),
                                delegate_calls=delegation_info.get("delegate_calls", 0),
                                required_experts_complete=delegation_info.get(
                                    "required_experts_complete", False
                                ),
                                budget_blocked=delegation_info.get("budget_blocked", []),
                            )
                        audit = copy.deepcopy(calls[start:])
                        grade = grade_turn(
                            turn,
                            body,
                            status_code,
                            audit,
                            before,
                            after,
                            index,
                            task.forbidden_claims,
                            known_order_ids,
                            session_calls,
                        )
                        session_calls.extend(audit)
                        unavailable = status_code >= 500 or body.get("status") in (
                            "failed",
                            "budget_exhausted",
                        )
                        turns.append(
                            {
                                "turn": n,
                                "http_status": status_code,
                                "request": payload,
                                "response": body,
                                "wall_elapsed_ms": elapsed_ms,
                                "calls": audit,
                                "status": "error"
                                if unavailable
                                else "failed"
                                if grade["failures"]
                                else "passed",
                                **grade,
                            }
                        )
                    failed = any(t["status"] in ("failed", "blocked") for t in turns)
                    errored = any(t["status"] == "error" for t in turns)
                    safety_failed = any(t.get("safety_failures") for t in turns)
                    review = bool(task.semantic_review) or any(t["review_flags"] for t in turns)
                    results.append(
                        {
                            "id": task.id,
                            "source_id": task.source_id,
                            "derived_from": task.derived_from,
                            "variant": task.variant,
                            "scenario": task.scenario,
                            "split": task.split,
                            "split_group": task.split_group,
                            "deterministic_status": "failed"
                            if safety_failed or failed and not errored
                            else "not_evaluated"
                            if errored
                            else "passed",
                            "semantic_status": "pending" if review else "not_required",
                            "task_status": "failed"
                            if safety_failed
                            else "error"
                            if errored
                            else "failed"
                            if failed
                            else "review_required"
                            if review
                            else "passed",
                            "review_criterion": task.semantic_review,
                            "turns": turns,
                        }
                    )
                    state = task_monitor_state[task.id]
                    specialist_agents = sorted(state["specialist_agents"])
                    if delegation_policy == "required_when_eligible":
                        delegation_expected = bool(state["required_experts"])
                        actual_calls = max(
                            state["delegate_calls"], state["delegate_dispatch_count"]
                        )
                        delegation_used = actual_calls > 0 or bool(specialist_agents)
                        delegation_status = (
                            "required_complete"
                            if delegation_expected
                            and state["required_experts"] <= state["completed_experts"]
                            else "required_incomplete"
                            if delegation_expected
                            else "not_required"
                        )
                    else:
                        delegation_expected = False
                        delegation_used = bool(state["delegation_call_ids"] or specialist_agents)
                        delegation_status = "used" if delegation_used else "not_expected"
                    log_progress(
                        "task_finished",
                        task_id=task.id,
                        status=results[-1]["task_status"],
                        delegation_expected=delegation_expected,
                        delegation_used=delegation_used,
                        delegation_calls=(
                            max(state["delegate_calls"], state["delegate_dispatch_count"])
                            if delegation_policy == "required_when_eligible"
                            else state["delegate_calls"] or len(state["delegation_call_ids"])
                        ),
                        specialist_agents=specialist_agents,
                        delegation_status=delegation_status,
                        required_experts=sorted(state["required_experts"]),
                        completed_experts=sorted(state["completed_experts"]),
                        budget_blocked=sorted(state["budget_blocked_experts"]),
                        real_delegate_calls=(
                            max(state["delegate_calls"], state["delegate_dispatch_count"])
                            if delegation_policy == "required_when_eligible"
                            else state["delegate_calls"]
                        ),
                    )
        finally:
            await c.aclose()
    for case in results:
        case["response_digest"] = response_digest(case)
    executed = {r["id"] for r in results}
    manifest = (
        json.loads((DATA_DIR / "agent_task_splits.json").read_text())
        if dataset_path is None
        else {
            "method": "Independent synthetic benchmark: deterministic, case-isolated 80/20 development/holdout split."
        }
    )
    digest = (
        corpus_digest()
        if dataset_path is None
        else hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()
    )
    return {
        "schema_version": 1,
        "corpus_digest": digest,
        "mode": "real_model" if live else "deterministic_offline",
        "provider": provider if live else "offline",
        "delegation_enabled": delegation_enabled,
        "delegation_policy": delegation_policy,
        "delegation_prompt_version": PROMPT_VERSION
        if delegation_policy == "required_when_eligible"
        else None,
        "arm": arm,
        "agent_context_limit_tokens": min(settings.agent_context_tokens, 32_000),
        "agent_total_budget_tokens": settings.agent_total_tokens,
        "model": (settings.agnes_model if live and provider == "agnes" else settings.deepseek_model)
        if live
        else "offline",
        "thinking": settings.deepseek_thinking if live and provider == "deepseek" else None,
        "selected_split": split,
        "corpus": corpus_summary(all_tasks),
        "selected": corpus_summary(selected),
        "unrun_count": len(all_tasks) - len(executed),
        "grouping_method": manifest["method"],
        "scope": "Derived tasks and turns are correlated members of source families, not independent new samples. Semantic review is pending, never silently treated as passed.",
        "monitoring": {
            "enabled": monitor,
            "event_count": len(monitor_events),
            "events": monitor_events,
        },
        **summarize(results, all_tasks),
        "cases": results,
    }
