"""The pre-evaluation task partition must remain reproducible and source-locked."""

import json

from eval.agent_ab import load_frozen_partition, paired_comparison
from scripts.freeze_agent_task_ab_partition import OUTPUT_JSON, OUTPUT_MD, build


def test_frozen_ab_partition_matches_reviewed_sources():
    expected, expected_markdown = build()

    assert json.loads(OUTPUT_JSON.read_text(encoding="utf-8")) == expected
    assert OUTPUT_MD.read_text(encoding="utf-8") == expected_markdown
    assert expected["summary"]["development_tasks"] == 135
    assert expected["summary"]["explicit_evidence_candidates"] == 52
    assert expected["summary"]["manual_policy_reasoning_additions"] == 14
    assert expected["summary"]["ab_comparison"] == 66
    assert expected["summary"]["b_only"] == 69
    assert expected["summary"]["holdout_excluded"] == 31
    assert expected["evaluation_started"] is False
    assert expected["holdout_executed"] is False


def test_ab_runner_loads_the_frozen_development_corpus_and_excludes_holdout():
    frozen = load_frozen_partition()

    assert len(frozen["tasks"]) == 166
    assert len(frozen["development_ids"]) == 135
    assert len(frozen["comparison_ids"]) == 66
    assert not set(frozen["development_ids"]) & {
        entry["id"] for entry in frozen["manifest"]["entries"] if entry["split"] == "holdout"
    }


def test_paired_report_compares_quality_only_on_shared_ab_tasks():
    def case(status):
        return {
            "id": "case-1",
            "deterministic_status": status,
            "turns": [
                {
                    "policy_quality": {"status": "passed", "evidence_checks": []},
                    "faithfulness_quality": {"status": "passed"},
                    "response": {},
                }
            ],
        }

    paired = paired_comparison({"cases": [case("failed")]}, {"cases": [case("passed")]}, ["case-1"])

    assert paired["n"] == 1
    assert paired["outcome_counts"] == {
        "a_only_passed": 0,
        "both_passed": 0,
        "b_only_passed": 1,
        "neither_passed": 0,
    }
    assert paired["quality"]["B"]["faithfulness"]["pass_rate_among_judged"] == 1.0
