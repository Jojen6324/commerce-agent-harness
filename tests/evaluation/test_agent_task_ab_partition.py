"""The pre-evaluation task partition must remain reproducible and source-locked."""

import json

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
