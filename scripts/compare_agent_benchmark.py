"""Paired A/B evaluation on the SAME tasks.

Examples:
 python -m scripts.compare_agent_benchmark --split development --limit 3 --live
 python -m scripts.compare_agent_benchmark --split development --limit 3 --live --monitor
 python -m scripts.compare_agent_benchmark --split development --limit 3 --live --provider deepseek --monitor
 python -m scripts.compare_agent_benchmark --split all --live
 python -m scripts.compare_agent_benchmark --split all   # offline plumbing smoke; NOT model quality
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from eval.behavior import evaluate
from eval.task_schema import DATA_DIR

DATASET = DATA_DIR / "agent_comparison_50.jsonl"
DEFAULT_OUTPUT = Path("eval/reports/agent_comparison_50_ab.json")
ARM_CONFIG = {
    "A": (False, "off"),
    "B": (True, "required_when_eligible"),
}


def summarize_variant(report: dict) -> dict:
    cases = report["cases"]
    calls = [call for case in cases for turn in case["turns"] for call in turn.get("calls", [])]
    turns = [turn for case in cases for turn in case["turns"]]
    durations = [
        turn["wall_elapsed_ms"] for turn in turns if turn.get("wall_elapsed_ms") is not None
    ]
    task_events = [
        event
        for event in report.get("monitoring", {}).get("events", [])
        if event.get("event") == "task_finished"
    ]
    monitored_calls = sum(event.get("real_delegate_calls", 0) or 0 for event in task_events)
    expected = [event for event in task_events if event.get("delegation_expected")]
    response_turns = [
        turn for case in cases for turn in case["turns"] if isinstance(turn.get("response"), dict)
    ]
    role_usage: dict[str, dict[str, int]] = {}
    required_experts = set()
    completed_experts = set()
    blocked_experts = set()
    real_delegate_calls = 0
    required_turns = 0
    completed_required_turns = 0
    diagnostic_expected_tasks = 0
    diagnostic_used_tasks = 0
    diagnostic_missing_tasks = 0
    diagnostic_incomplete_tasks = 0
    total_tokens = model_calls = 0
    for case in cases:
        task_required: set[str] = set()
        task_completed: set[str] = set()
        task_delegate_calls = 0
        for turn in case["turns"]:
            response = turn.get("response")
            if not isinstance(response, dict):
                continue
            delegation = (response.get("diagnostics") or {}).get("delegation") or {}
            task_required.update(delegation.get("required_experts", []))
            task_completed.update(delegation.get("completed_experts", []))
            task_delegate_calls += delegation.get("delegate_calls", 0) or 0
        if task_required:
            diagnostic_expected_tasks += 1
            diagnostic_used_tasks += task_delegate_calls > 0 or bool(task_completed)
            diagnostic_missing_tasks += task_delegate_calls == 0 and not task_completed
            diagnostic_incomplete_tasks += not task_required <= task_completed
    for turn in response_turns:
        response = turn["response"]
        diagnostics = response.get("diagnostics") or {}
        delegation = diagnostics.get("delegation") or {}
        roles = diagnostics.get("role_usage") or {}
        real_delegate_calls += delegation.get("delegate_calls", 0) or 0
        required = set(delegation.get("required_experts", []))
        completed = set(delegation.get("completed_experts", []))
        required_experts.update(required)
        completed_experts.update(completed)
        blocked_experts.update(delegation.get("budget_blocked", []))
        if required:
            required_turns += 1
            completed_required_turns += required <= completed
        usage = response.get("token_usage") or {}
        total_tokens += usage.get("total_tokens", 0) or 0
        model_calls += diagnostics.get("model_calls", 0) or 0
        for role, values in roles.items():
            totals = role_usage.setdefault(
                role,
                {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            for key in totals:
                totals[key] += values.get(key, 0) or 0
    monitored_roles: dict[str, dict[str, int]] = {}
    for event in report.get("monitoring", {}).get("events", []):
        role = event.get("agent")
        if not role or event.get("event") not in ("model_started", "model_completed"):
            continue
        totals = monitored_roles.setdefault(
            role,
            {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        if event["event"] == "model_started":
            totals["model_calls"] += 1
        else:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[key] += event.get(key, 0) or 0
    for role, values in monitored_roles.items():
        totals = role_usage.setdefault(
            role,
            {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        for key in totals:
            totals[key] = max(totals[key], values[key])
    monitored_required = {
        role for event in task_events for role in event.get("required_experts", [])
    }
    monitored_completed = {
        role for event in task_events for role in event.get("completed_experts", [])
    }
    monitored_blocked = {role for event in task_events for role in event.get("budget_blocked", [])}
    required_experts.update(monitored_required)
    completed_experts.update(monitored_completed)
    blocked_experts.update(monitored_blocked)
    required_turns = max(
        required_turns,
        sum(bool(event.get("delegation_expected")) for event in task_events)
        if report.get("delegation_policy") == "required_when_eligible"
        else 0,
    )
    completed_required_turns = max(
        completed_required_turns,
        sum(event.get("delegation_status") == "required_complete" for event in task_events),
    )
    return {
        "n": len(cases),
        "turns": len(turns),
        "statuses": report["overall"]["task_status"],
        "deterministic_passed": report["overall"]["deterministic_passed"],
        "safety_violations": report["overall"]["safety_violation_count"],
        "tool_calls": len(calls),
        "delegation_dispatch_attempts": sum(call["name"] == "delegate_task" for call in calls),
        "non_delegation_tool_calls": sum(call["name"] != "delegate_task" for call in calls),
        "budget_rejected_dispatches": sum(call.get("error") == "BudgetExceeded" for call in calls),
        "delegation_enabled": report.get("delegation_enabled"),
        "delegation_policy": report.get("delegation_policy"),
        "agent_context_limit_tokens": report.get("agent_context_limit_tokens"),
        "agent_total_budget_tokens": report.get("agent_total_budget_tokens"),
        "delegation_prompt_version": report.get("delegation_prompt_version"),
        "real_delegate_calls": max(real_delegate_calls, monitored_calls),
        "required_expert_roles": sorted(required_experts),
        "completed_expert_roles": sorted(completed_experts),
        "budget_blocked_expert_roles": sorted(blocked_experts),
        "required_expert_turns": required_turns,
        "required_expert_turns_completed": completed_required_turns,
        "model_calls": model_calls,
        "total_tokens": total_tokens,
        "role_usage": role_usage,
        "delegation_expected_tasks": max(len(expected), diagnostic_expected_tasks),
        "delegation_used_tasks": max(
            sum(bool(event.get("delegation_used")) for event in expected), diagnostic_used_tasks
        ),
        "delegation_missing_tasks": max(
            sum(event.get("delegation_status") == "missing" for event in expected),
            diagnostic_missing_tasks,
        ),
        "delegation_incomplete_tasks": max(
            sum(event.get("delegation_status") == "required_incomplete" for event in expected),
            diagnostic_incomplete_tasks,
        ),
        "specialist_agent_starts": sum(
            1
            for event in report.get("monitoring", {}).get("events", [])
            if event.get("event") == "agent_started"
            and event.get("agent") in ("orders", "policy", "knowledge")
        ),
        "turn_wall_elapsed_ms": {
            "total": round(sum(durations), 2),
            "mean": round(sum(durations) / len(durations), 2) if durations else None,
            "max": max(durations) if durations else None,
        },
        "mode": report["mode"],
    }


def pair_results(single: dict, multi: dict) -> list[dict]:
    reports = [single, multi]
    indexed = [{case["id"]: case for case in report["cases"]} for report in reports]
    if any(set(item) != set(indexed[0]) for item in indexed[1:]):
        raise ValueError("A/B case ids differ")
    if any(report["corpus_digest"] != single["corpus_digest"] for report in reports[1:]):
        raise ValueError("A/B benchmark dataset differs")
    left, right = indexed[:2]
    return [
        {
            "id": key,
            "scenario": left[key]["scenario"],
            "A": left[key]["task_status"],
            "B": right[key]["task_status"],
            "A_deterministic": left[key]["deterministic_status"],
            "B_deterministic": right[key]["deterministic_status"],
            "A_safety_failures": sum(len(t.get("safety_failures", [])) for t in left[key]["turns"]),
            "B_safety_failures": sum(
                len(t.get("safety_failures", [])) for t in right[key]["turns"]
            ),
            "A_turn_wall_elapsed_ms": [t.get("wall_elapsed_ms") for t in left[key]["turns"]],
            "B_turn_wall_elapsed_ms": [t.get("wall_elapsed_ms") for t in right[key]["turns"]],
        }
        for key in left
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "holdout", "all"], default="development")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--live", action="store_true", help="Use configured AGNES; may incur cost or rate limits"
    )
    parser.add_argument(
        "--provider",
        choices=["agnes", "deepseek"],
        default="agnes",
        help="Explicit live model provider",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Stream per-turn duration, delegation mode/use, context, model, tool and validation progress to stderr",
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=["A", "B"],
        default=["A", "B"],
        help="Run A (single agent) and/or B (required Policy Agent for retrieval, optional Orders Agent)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    options = dict(
        dataset_path=DATASET,
        live=args.live,
        split=args.split,
        limit=args.limit,
        monitor=args.monitor,
        provider=args.provider,
    )
    # Each evaluation gets the same tasks, model, 32K context and 80K run cap;
    # only the delegation policy changes. Fixtures are reinstalled per case.
    reports = {}
    for arm in args.arms:
        enabled, policy = ARM_CONFIG[arm]
        reports[arm] = await evaluate(
            **options,
            delegation_enabled=enabled,
            delegation_policy=policy,
            arm_name=arm,
        )
    a, b = reports.get("A"), reports.get("B")
    paired = pair_results(a, b) if a and b else []
    reference = next(iter(reports.values()))
    data = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": str(DATASET),
        "digest": reference["corpus_digest"],
        "split": args.split,
        "limit": args.limit,
        "model_execution": "real_model" if args.live else "offline_smoke",
        "provider": reference["provider"],
        "model": reference["model"],
        "thinking": reference["thinking"],
        "caveat": (
            "The offline mode verifies harness plumbing only and cannot establish agent quality. "
            "Passing deterministic checks is not semantic approval. No statistical significance "
            "is implied by this small, partly correlated synthetic task corpus. "
            "A is single-agent. B requires Policy Agent retrieval when live request/history/facts indicate a need for policy evidence; Main Agent may optionally delegate order facts to Orders. Dataset scenario labels are reporting metadata only. "
            "Monitoring reports requested versus completed specialists and never stops a run automatically."
        ),
        "a_summary": summarize_variant(a) if a else None,
        "b_summary": summarize_variant(b) if b else None,
        "paired": paired,
        "arms": {key: summarize_variant(report) for key, report in reports.items()},
        "a_report": a,
        "b_report": b,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: data[k]
                for k in ("split", "model_execution", "provider", "model", "thinking", "arms")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Report: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
