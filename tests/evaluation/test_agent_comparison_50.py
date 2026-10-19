"""Schema and pairing regression for the separate agent comparison corpus."""
import json
import pytest

from eval.task_schema import AgentTask, DATA_DIR
from eval.behavior import evaluate
from scripts.compare_agent_benchmark import pair_results

DATASET = DATA_DIR / "agent_comparison_50.jsonl"


def test_comparison_corpus_is_well_formed():
    tasks = [AgentTask.model_validate_json(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
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
        return {"corpus_digest": "same", "cases": [
            {"id": i, "scenario": "multi_agent", "task_status": "passed",
             "deterministic_status": "passed", "turns": []} for i in ids
        ]}
    assert len(pair_results(report(["a"]), report(["a"]))) == 1
    with pytest.raises(ValueError):
        pair_results(report(["a"]), report(["b"]))


@pytest.mark.asyncio
async def test_monitor_records_turn_wall_time_and_expected_vs_actual_delegation():
    report = await evaluate(
        dataset_path=DATASET,
        split="development",
        limit=1,
        delegation_enabled=True,
        monitor=True,
    )
    case = report["cases"][0]
    task_events = [
        event
        for event in report["monitoring"]["events"]
        if event["event"] == "task_finished" and event["task_id"] == case["id"]
    ]
    assert report["delegation_enabled"] is True
    assert case["turns"][0]["wall_elapsed_ms"] >= 0
    assert len(task_events) == 1
    assert task_events[0]["delegation_expected"] is True
    assert task_events[0]["delegation_used"] is False
    assert task_events[0]["delegation_status"] == "missing"
