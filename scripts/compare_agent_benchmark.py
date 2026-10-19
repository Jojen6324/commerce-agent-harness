"""Paired evaluation of the SAME tasks with delegation off (single) and on (multi).

Examples:
 python -m scripts.compare_agent_benchmark --split development --limit 3 --live
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


def summarize_variant(report: dict) -> dict:
    cases = report["cases"]
    calls = [call for case in cases for turn in case["turns"] for call in turn.get("calls", [])]
    return {
        "n": len(cases),
        "statuses": report["overall"]["task_status"],
        "deterministic_passed": report["overall"]["deterministic_passed"],
        "safety_violations": report["overall"]["safety_violation_count"],
        "tool_calls": len(calls),
        "mode": report["mode"],
    }


def pair_results(single: dict, multi: dict) -> list[dict]:
    left = {case["id"]: case for case in single["cases"]}
    right = {case["id"]: case for case in multi["cases"]}
    if set(left) != set(right):
        raise ValueError("A/B case ids differ")
    if single["corpus_digest"] != multi["corpus_digest"]:
        raise ValueError("A/B benchmark dataset differs")
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
        }
        for key in left
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "holdout", "all"], default="development")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--live", action="store_true", help="Use configured AGNES; may incur cost or rate limits")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    options = dict(dataset_path=DATASET, live=args.live, split=args.split, limit=args.limit)
    # Each evaluation creates its own temporary DB and reinstalls fixtures before EVERY case.
    a = await evaluate(**options, delegation_enabled=False)
    b = await evaluate(**options, delegation_enabled=True)
    paired = pair_results(a, b)
    data = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": str(DATASET),
        "digest": a["corpus_digest"],
        "split": args.split,
        "limit": args.limit,
        "model_execution": "real_model" if args.live else "offline_smoke",
        "caveat": (
            "The offline mode verifies harness plumbing only and cannot establish agent quality. "
            "Passing deterministic checks is not semantic approval. No statistical significance "
            "is implied by this small, partly correlated synthetic task corpus. "
            "Delegation enabled does not guarantee that the model actually delegates."
        ),
        "single_summary": summarize_variant(a),
        "multi_summary": summarize_variant(b),
        "paired": paired,
        "single_report": a,
        "multi_report": b,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: data[k] for k in ("split", "model_execution", "single_summary", "multi_summary")}, ensure_ascii=False, indent=2))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
