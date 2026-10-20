"""Run the frozen development A/B partition without touching Holdout tasks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from eval.behavior import evaluate, quality_summary, resource_summary
from eval.task_schema import DATA_DIR, AgentTask

ROOT = Path(__file__).resolve().parents[1]
PARTITION_PATH = DATA_DIR / "agent_task_ab_partition.json"
REPORT_DIR = ROOT / "eval/reports"


def load_frozen_partition():
    """Verify source hashes and return the exact frozen corpus and task groups."""
    partition_bytes = PARTITION_PATH.read_bytes()
    manifest = json.loads(partition_bytes)
    tasks = []
    source_bytes = {}
    for relative_path, source in manifest["sources"].items():
        path = ROOT / relative_path
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError(f"frozen A/B source changed: {relative_path}")
        source_bytes[relative_path] = raw
        rows = [AgentTask.model_validate_json(line) for line in raw.splitlines() if line.strip()]
        if len(rows) != source["total"]:
            raise ValueError(f"frozen A/B source count changed: {relative_path}")
        tasks.extend(rows)

    entries = manifest["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    if len(by_id) != len(entries) or {task.id for task in tasks} != set(by_id):
        raise ValueError("partition manifest does not match the source task IDs")
    for task in tasks:
        entry = by_id[task.id]
        if (
            task.source_id != entry["source_id"]
            or task.split != entry["split"]
            or task.split_group != entry["split_group"]
            or task.scenario != entry["scenario"]
        ):
            raise ValueError(f"partition metadata drift for task {task.id}")

    development = [entry for entry in entries if entry["split"] == "development"]
    comparison = [entry for entry in development if entry["group"] == "ab_comparison"]
    b_only = [entry for entry in development if entry["group"] == "b_only"]
    if (
        len(tasks) != manifest["summary"]["total_records"]
        or len(development) != 135
        or len(comparison) != 66
        or len(b_only) != 69
        or any(
            entry["split"] == "holdout" and entry["group"] != "holdout_excluded"
            for entry in entries
        )
    ):
        raise ValueError("frozen A/B partition counts or Holdout exclusion changed")

    digest = hashlib.sha256()
    for relative_path in sorted(source_bytes):
        digest.update(relative_path.encode())
        digest.update(source_bytes[relative_path])
    digest.update(partition_bytes)
    return {
        "manifest": manifest,
        "tasks": tasks,
        "comparison_ids": [entry["id"] for entry in comparison],
        "development_ids": [entry["id"] for entry in development],
        "corpus_digest": digest.hexdigest(),
        "partition_digest": hashlib.sha256(partition_bytes).hexdigest(),
    }


def paired_comparison(a_report, b_report, comparison_ids):
    a_cases = {case["id"]: case for case in a_report["cases"]}
    b_cases = {case["id"]: case for case in b_report["cases"]}
    if set(a_cases) != set(comparison_ids) or not set(comparison_ids) <= set(b_cases):
        raise ValueError("A/B results do not match the frozen paired task IDs")
    a_pairs = [a_cases[task_id] for task_id in comparison_ids]
    b_pairs = [b_cases[task_id] for task_id in comparison_ids]
    outcomes = Counter(
        {
            "both_passed": 0,
            "b_only_passed": 0,
            "a_only_passed": 0,
            "neither_passed": 0,
        }
    )
    deterministic = Counter()
    for task_id in comparison_ids:
        a_status = a_cases[task_id]["deterministic_status"]
        b_status = b_cases[task_id]["deterministic_status"]
        a_pass = a_status == "passed"
        b_pass = b_status == "passed"
        outcomes[
            "both_passed"
            if a_pass and b_pass
            else "b_only_passed"
            if b_pass
            else "a_only_passed"
            if a_pass
            else "neither_passed"
        ] += 1
        deterministic[f"A_{a_status}__B_{b_status}"] += 1
    return {
        "n": len(comparison_ids),
        "outcome_counts": dict(sorted(outcomes.items())),
        "deterministic_status_transitions": dict(sorted(deterministic.items())),
        "quality": {
            "A": quality_summary(a_pairs),
            "B": quality_summary(b_pairs),
        },
        "resources": {
            "A": resource_summary(a_pairs),
            "B": resource_summary(b_pairs),
        },
    }


async def evaluate_frozen_partition(
    *, live=False, provider="agnes", monitor=False, container_factory=None
):
    frozen = load_frozen_partition()
    common = {
        "live": live,
        "split": "development",
        "task_corpus": frozen["tasks"],
        "corpus_digest_override": frozen["corpus_digest"],
        "grouping_method_override": "Frozen source_id-family A/B partition; Holdout excluded.",
        "provider": provider,
        "monitor": monitor,
    }
    if container_factory is not None:
        common["container_factory"] = container_factory

    a_report = await evaluate(
        **common,
        task_ids=frozen["comparison_ids"],
        delegation_enabled=False,
        delegation_policy="off",
        arm_name="A",
    )
    b_report = await evaluate(
        **common,
        task_ids=frozen["development_ids"],
        delegation_enabled=True,
        delegation_policy="required_when_eligible",
        arm_name="B",
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "real_model" if live else "deterministic_offline",
        "provider": provider if live else "offline",
        "selection": {
            "partition_manifest_sha256": frozen["partition_digest"],
            "corpus_digest": frozen["corpus_digest"],
            "development_tasks": len(frozen["development_ids"]),
            "ab_comparison_tasks": len(frozen["comparison_ids"]),
            "b_only_tasks": 69,
            "holdout_tasks_excluded": 31,
            "holdout_executed": False,
        },
        "arms": {"A": a_report, "B": b_report},
        "paired_comparison": paired_comparison(a_report, b_report, frozen["comparison_ids"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="Use the configured live provider")
    parser.add_argument("--provider", choices=("agnes", "deepseek", "qwen1"), default="agnes")
    parser.add_argument("--monitor", action="store_true", help="Print live run events")
    args = parser.parse_args()
    report = asyncio.run(
        evaluate_frozen_partition(live=args.llm, provider=args.provider, monitor=args.monitor)
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"agent_ab_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
