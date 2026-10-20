"""Paired evaluation of the SAME tasks in A/B/C delegation modes.

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
DEFAULT_OUTPUT = Path("eval/reports/agent_comparison_50_abc.json")


def summarize_variant(report: dict) -> dict:
    cases = report["cases"]
    calls = [call for case in cases for turn in case["turns"] for call in turn.get("calls", [])]
    turns = [turn for case in cases for turn in case["turns"]]
    durations = [turn["wall_elapsed_ms"] for turn in turns if turn.get("wall_elapsed_ms") is not None]
    task_events = [
        event
        for event in report.get("monitoring", {}).get("events", [])
        if event.get("event") == "task_finished"
    ]
    monitored_calls = sum(event.get("real_delegate_calls", 0) or 0 for event in task_events)
    expected = [event for event in task_events if event.get("delegation_expected")]
    response_turns = [
        turn
        for case in cases
        for turn in case["turns"]
        if isinstance(turn.get("response"), dict)
    ]
    role_usage: dict[str, dict[str, int]] = {}
    required_experts = set()
    completed_experts = set()
    blocked_experts = set()
    real_delegate_calls = 0
    required_turns = 0
    completed_required_turns = 0
    total_tokens = model_calls = 0
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
    monitored_required = {role for event in task_events for role in event.get("required_experts", [])}
    monitored_completed = {role for event in task_events for role in event.get("completed_experts", [])}
    monitored_blocked = {role for event in task_events for role in event.get("budget_blocked", [])}
    required_experts.update(monitored_required)
    completed_experts.update(monitored_completed)
    blocked_experts.update(monitored_blocked)
    required_turns = max(
        required_turns,
        sum(bool(event.get("delegation_expected")) for event in task_events)
        if report.get("arm") == "C"
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
        "delegation_expected_tasks": len(expected),
        "delegation_used_tasks": sum(bool(event.get("delegation_used")) for event in expected),
        "delegation_missing_tasks": sum(
            event.get("delegation_status") == "missing" for event in expected
        ),
        "delegation_incomplete_tasks": sum(
            event.get("delegation_status") == "required_incomplete" for event in expected
        ),
        "specialist_agent_starts": sum(
            1
            for event in report.get("monitoring", {}).get("events", [])
            if event.get("event") == "agent_started" and event.get("agent") != "main"
        ),
        "turn_wall_elapsed_ms": {
            "total": round(sum(durations), 2),
            "mean": round(sum(durations) / len(durations), 2) if durations else None,
            "max": max(durations) if durations else None,
        },
        "mode": report["mode"],
    }


def pair_results(single: dict, multi: dict, c_mode: dict | None = None) -> list[dict]:
    reports = [single, multi] + ([c_mode] if c_mode is not None else [])
    indexed = [{case["id"]: case for case in report["cases"]} for report in reports]
    if any(set(item) != set(indexed[0]) for item in indexed[1:]):
        raise ValueError("A/B/C case ids differ")
    if any(report["corpus_digest"] != single["corpus_digest"] for report in reports[1:]):
        raise ValueError("A/B/C benchmark dataset differs")
    left, right = indexed[:2]
    third = indexed[2] if len(indexed) > 2 else None
    return [
        {
            "id": key,
            "scenario": left[key]["scenario"],
            "single": left[key]["task_status"],
            "multi": right[key]["task_status"],
            "single_deterministic": left[key]["deterministic_status"],
            "multi_deterministic": right[key]["deterministic_status"],
            "single_safety_failures": sum(len(t.get("safety_failures", [])) for t in left[key]["turns"]),
            "multi_safety_failures": sum(len(t.get("safety_failures", [])) for t in right[key]["turns"]),
            "single_turn_wall_elapsed_ms": [t.get("wall_elapsed_ms") for t in left[key]["turns"]],
            "multi_turn_wall_elapsed_ms": [t.get("wall_elapsed_ms") for t in right[key]["turns"]],
            **(
                {
                    "c": third[key]["task_status"],
                    "c_deterministic": third[key]["deterministic_status"],
                    "c_turn_wall_elapsed_ms": [t.get("wall_elapsed_ms") for t in third[key]["turns"]],
                }
                if third is not None
                else {}
            ),
        }
        for key in left
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "holdout", "all"], default="development")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--live", action="store_true", help="Use configured AGNES; may incur cost or rate limits")
    parser.add_argument("--provider", choices=["agnes", "deepseek"], default="agnes", help="Explicit live model provider")
    parser.add_argument("--monitor", action="store_true", help="Stream per-turn duration, delegation mode/use, context, model, tool and validation progress to stderr")
    parser.add_argument("--arms", nargs="+", choices=["A", "B", "C"], default=["A", "B", "C"], help="Run A (delegation off), B (model-directed delegation), C (required evidence preflight)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    options = dict(dataset_path=DATASET, live=args.live, split=args.split, limit=args.limit, monitor=args.monitor, provider=args.provider)
    # Each evaluation gets the same tasks, model, 32K context and 80K run cap;
    # only the delegation policy changes. Fixtures are reinstalled per case.
    config = {
        "A": (False, "off"),
        "B": (True, "auto"),
        "C": (True, "required_when_eligible"),
    }
    reports = {}
    for arm in args.arms:
        enabled, policy = config[arm]
        reports[arm] = await evaluate(
            **options,
            delegation_enabled=enabled,
            delegation_policy=policy,
            arm_name=arm,
        )
    a, b, c_mode = reports.get("A"), reports.get("B"), reports.get("C")
    paired = pair_results(a, b, c_mode) if a and b else []
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
            "B leaves delegation to the model. C selects required evidence experts from the live request, history and confirmed facts; dataset scenario labels are used only for reporting. "
            "Monitoring reports requested versus completed specialists and never stops a run automatically."
        ),
        "single_summary": summarize_variant(a) if a else None,
        "multi_summary": summarize_variant(b) if b else None,
        "c_summary": summarize_variant(c_mode) if c_mode else None,
        "paired": paired,
        "arms": {key: summarize_variant(report) for key, report in reports.items()},
        "single_report": a,
        "multi_report": b,
        "c_report": c_mode,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: data[k] for k in ("split", "model_execution", "provider", "model", "thinking", "arms")}, ensure_ascii=False, indent=2))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
