"""API task evaluation with source-family splits; no intent-to-tool mapping."""

from __future__ import annotations

import copy
import json
import re
import tempfile
from collections import Counter, defaultdict
from contextvars import ContextVar

import httpx
from pydantic import SecretStr

from app.config.settings import Settings
from app.main import create_app
from app.services.container import build_container
from eval.task_fixtures import install_fixture, matches, snapshot
from eval.task_grading import grade_turn
from eval.task_review import corpus_digest, response_digest
from eval.task_schema import DATA_DIR, load_tasks


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
    *, live=False, limit=None, split="development", task_ids=None, container_factory=build_container
):
    all_tasks = load_tasks()
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
    results = []
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
            settings = settings.model_copy(update={"agnes": SecretStr("")})
        if live and not settings.agnes.get_secret_value():
            raise RuntimeError("AGNES binding required for live evaluation")
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
                    await install_fixture(c.session_factory, task.fixtures)
                    calls.clear()
                    session_id = None
                    responses = []
                    turns = []
                    session_calls = []
                    known_order_ids = set()
                    for n, turn in enumerate(task.turns, 1):
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
                        response = await client.post("/api/v1/chat", json=payload)
                        try:
                            body = response.json()
                        except ValueError:
                            body = {"error": "non_json_api_response"}
                        responses.append(body)
                        if response.status_code == 200:
                            session_id = body.get("session_id", session_id)
                        after = await snapshot(c.session_factory)
                        audit = copy.deepcopy(calls[start:])
                        grade = grade_turn(
                            turn,
                            body,
                            response.status_code,
                            audit,
                            before,
                            after,
                            index,
                            task.forbidden_claims,
                            known_order_ids,
                            session_calls,
                        )
                        session_calls.extend(audit)
                        unavailable = response.status_code >= 500 or body.get("status") in (
                            "failed",
                            "budget_exhausted",
                        )
                        turns.append(
                            {
                                "turn": n,
                                "http_status": response.status_code,
                                "request": payload,
                                "response": body,
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
        finally:
            await c.aclose()
    for case in results:
        case["response_digest"] = response_digest(case)
    executed = {r["id"] for r in results}
    manifest = json.loads((DATA_DIR / "agent_task_splits.json").read_text())
    return {
        "schema_version": 1,
        "corpus_digest": corpus_digest(),
        "mode": "real_model" if live else "deterministic_offline",
        "selected_split": split,
        "corpus": corpus_summary(all_tasks),
        "selected": corpus_summary(selected),
        "unrun_count": len(all_tasks) - len(executed),
        "grouping_method": manifest["method"],
        "scope": "Derived tasks and turns are correlated members of source families, not independent new samples. Semantic review is pending, never silently treated as passed.",
        **summarize(results, all_tasks),
        "cases": results,
    }
