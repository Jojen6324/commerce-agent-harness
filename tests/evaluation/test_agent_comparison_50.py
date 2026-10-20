"""Schema and pairing regression for the separate agent comparison corpus."""

import json

import pytest

from eval.behavior import evaluate
from eval.task_schema import DATA_DIR, AgentTask
from scripts.compare_agent_benchmark import ARM_CONFIG, pair_results, summarize_variant

DATASET = DATA_DIR / "agent_comparison_50.jsonl"


def test_comparison_corpus_is_well_formed():
    tasks = [
        AgentTask.model_validate_json(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(tasks) == len({t.id for t in tasks}) == 50
    assert len([t for t in tasks if t.scenario == "multi_agent"]) == 30
    assert len([t for t in tasks if t.scenario == "multi_turn"]) == 15
    assert len([t for t in tasks if t.scenario == "exception"]) == 5
    assert sum(len(t.turns) for t in tasks) == 70
    assert sum(t.split == "holdout" for t in tasks) == 10
    assert len({t.turns[0].message for t in tasks}) == 50
    fixtures = set(json.loads((DATA_DIR / "agent_fixtures.json").read_text())["profiles"])
    assert all(set(t.fixtures) <= fixtures for t in tasks)
    for task in tasks:
        for i, turn in enumerate(task.turns, 1):
            assert turn.checks
            assert turn.confirm_from_turn is None or turn.confirm_from_turn < i
            assert all(c.kind != "proposal" for c in turn.checks) or not turn.allowed_changes


def test_comparison_requires_same_ids_and_digest():
    def report(ids):
        return {
            "corpus_digest": "same",
            "cases": [
                {
                    "id": i,
                    "scenario": "multi_agent",
                    "task_status": "passed",
                    "deterministic_status": "passed",
                    "turns": [],
                }
                for i in ids
            ],
        }

    assert len(pair_results(report(["a"]), report(["a"]))) == 1
    with pytest.raises(ValueError):
        pair_results(report(["a"]), report(["b"]))


def test_comparison_pairs_only_single_agent_a_and_policy_required_b():
    def report(status):
        return {
            "corpus_digest": "same",
            "cases": [
                {
                    "id": "task",
                    "scenario": "multi_agent",
                    "task_status": status,
                    "deterministic_status": status,
                    "turns": [],
                }
            ],
        }

    paired = pair_results(report("passed"), report("failed"))
    assert paired[0]["A"] == "passed"
    assert paired[0]["B"] == "failed"
    assert ARM_CONFIG == {"A": (False, "off"), "B": (True, "auto")}


def test_b_summary_derives_required_policy_usage_without_monitor_stream():
    response = {
        "diagnostics": {
            "delegation": {
                "required_experts": ["policy"],
                "completed_experts": ["policy"],
                "budget_blocked": [],
                "delegate_calls": 1,
            },
            "role_usage": {"policy": {"model_calls": 2, "total_tokens": 50}},
            "model_calls": 3,
        },
        "token_usage": {"total_tokens": 60},
    }
    report = {
        "cases": [{"id": "task", "turns": [{"response": response}]}],
        "monitoring": {"events": []},
        "overall": {
            "task_status": {"passed": 1},
            "deterministic_passed": 1,
            "safety_violation_count": 0,
        },
        "delegation_enabled": True,
        "delegation_policy": "auto",
        "agent_context_limit_tokens": 32_000,
        "agent_total_budget_tokens": 80_000,
        "delegation_prompt_version": "test",
        "mode": "deterministic_offline",
    }

    summary = summarize_variant(report)
    assert summary["delegation_expected_tasks"] == 1
    assert summary["delegation_used_tasks"] == 1
    assert summary["delegation_incomplete_tasks"] == 0
    assert summary["required_expert_roles"] == ["policy"]


@pytest.mark.asyncio
async def test_monitor_records_turn_wall_time_and_expected_vs_actual_delegation():
    from sse_starlette.sse import AppStatus

    # SSE's shutdown event is process-global and may be bound to an earlier
    # pytest event loop; reset it so this test owns the event it creates.
    AppStatus.should_exit_event = None
    report = await evaluate(
        dataset_path=DATASET,
        split="development",
        limit=1,
        delegation_enabled=True,
        delegation_policy="auto",
        monitor=True,
    )
    case = report["cases"][0]
    task_events = [
        event
        for event in report["monitoring"]["events"]
        if event["event"] == "task_finished" and event["task_id"] == case["id"]
    ]
    assert report["delegation_enabled"] is True
    assert report["agent_context_limit_tokens"] == 32_000
    assert report["agent_total_budget_tokens"] == 80_000
    assert case["turns"][0]["wall_elapsed_ms"] >= 0
    assert len(task_events) == 1
    assert task_events[0]["delegation_expected"] is False
    assert task_events[0]["delegation_status"] == "not_expected"
