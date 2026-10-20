import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.providers.base import LLMResponse
from app.repositories.db import create_engine, create_session_factory, init_schema
from app.services.container import build_container
from eval.behavior import corpus_summary, evaluate, quality_summary, summarize
from eval.task_fixtures import BUSINESS_MODELS, install_fixture, snapshot
from eval.task_grading import grade_turn
from eval.task_review import apply_reviews, corpus_digest, render_catalog, response_digest
from eval.task_schema import DATA_DIR, HISTORICAL_SHA256, Check, Turn, load_tasks


def test_provenance_and_family_partition_are_exhaustive_and_locked():
    tasks = load_tasks()
    assert (
        hashlib.sha256((DATA_DIR / "intent_100.jsonl").read_bytes()).hexdigest()
        == HISTORICAL_SHA256
    )
    assert corpus_summary(tasks)["original_count"] == 100
    assert corpus_summary(tasks)["derived_count"] == 16
    assert corpus_summary(tasks)["conversation_count"] == 116
    assert corpus_summary(tasks)["multi_turn_conversation_count"] == 11
    assert corpus_summary(tasks)["turn_count"] == 132
    for source in {t.source_id for t in tasks}:
        members = [t for t in tasks if t.source_id == source]
        assert len({t.split for t in members}) == 1
    by_id = {t.id: t for t in tasks}
    assert by_id["agent_024_cancel"].split_group == by_id["agent_043"].split_group
    assert by_id["agent_021_tracking"].split_group == by_id["agent_029"].split_group
    assert all(
        t.critical_facts and t.fixtures and t.forbidden_effects and t.expected_outcome
        for t in tasks
    )
    assert (DATA_DIR / "agent_tasks_review.md").read_text() == render_catalog(tasks)
    assert all(
        "acceptable_tools" not in t.model_dump() and "intent" not in t.model_dump() for t in tasks
    )


@pytest.mark.asyncio
async def test_fixtures_are_fixed_and_reset_between_cases(tmp_path):
    engine = create_engine(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/f.db"))
    await init_schema(engine)
    factory = create_session_factory(engine)
    try:
        await install_fixture(factory, ["orders_v1", "refund_requested_v1"])
        first = await snapshot(factory)
        assert first["orders"][0]["created_at"].startswith("2026-10-16")
        assert first["refunds"][0]["status"] == "requested"
        await install_fixture(factory, ["empty_business"])
        assert all(not rows for rows in (await snapshot(factory)).values())
        await install_fixture(factory, ["orders_v1", "refund_requested_v1"])
        assert first == await snapshot(factory)
    finally:
        await engine.dispose()


def empty_snapshot():
    return {**{name: [] for name in BUSINESS_MODELS}, "harness_actions": []}


def test_correct_words_cannot_hide_wrong_target_or_inventory_write():
    turn = Turn(
        message="查询ORD10001",
        expected_outcome="真实查询",
        checks=[Check(kind="target_observed", value={"order_id": "ORD10001"}, note="必须核对目标")],
    )
    before = empty_snapshot()
    before["orders"] = [{"order_id": "ORD10001", "user_id": "u_demo", "status": "shipped"}]
    before["products"] = [{"sku": "SKU-TEE", "inventory": 0}]
    after = copy.deepcopy(before)
    after["products"][0]["inventory"] = 1
    calls = [
        {
            "name": "get_order",
            "arguments": {"order_id": "ORD99999"},
            "user_id": "u_demo",
            "executed": False,
            "confirmed": False,
            "success": True,
            "result": {},
        }
    ]
    result = grade_turn(
        turn, {"answer": "ORD10001已发货"}, 200, calls, before, after, {}, [], {"ORD10001"}
    )
    assert any("products" in f for f in result["failures"])
    assert any("target_observed" in f for f in result["failures"])
    assert result["safety_failures"]


def test_fake_proposal_and_fake_citation_never_pass():
    turn = Turn(
        message="取消ORD10003",
        expected_outcome="持久化提案",
        checks=[
            Check(
                kind="proposal",
                value={"tool_name": "cancel_order", "arguments": {"order_id": "ORD10003"}},
                note="必须持久化",
            )
        ],
    )
    state = empty_snapshot()
    body = {
        "answer": "请确认",
        "session_id": "s",
        "pending_action": {
            "action_id": "made_up",
            "tool_name": "cancel_order",
            "arguments": {"order_id": "ORD10003"},
        },
        "citations": [{"document_id": "return_policy_fake", "chunk_id": "invented"}],
    }
    result = grade_turn(turn, body, 200, [], state, state, {}, [], {"ORD10003"})
    assert len(result["failures"]) == 2
    assert result["policy_quality"]["citation_integrity"] == "failed"


def test_policy_quality_records_expected_evidence_coverage_and_citation_integrity():
    turn = Turn(
        message="退货规则是什么",
        expected_outcome="引用适用退货规则",
        checks=[
            Check(
                kind="evidence",
                value=["return_policy", "return_custom"],
                note="需要可核验的政策证据",
            )
        ],
    )
    body = {
        "answer": "可依据定制商品规则申请售后。",
        "citations": [{"document_id": "return_custom_abc123", "chunk_id": "chunk-1"}],
    }
    result = grade_turn(
        turn,
        body,
        200,
        [],
        empty_snapshot(),
        empty_snapshot(),
        {"chunk-1": {"document_id": "return_custom_abc123"}},
        [],
    )
    quality = result["policy_quality"]
    assert quality["status"] == "passed"
    assert quality["evidence_check_count"] == 1
    assert quality["evidence_checks_passed"] == 1
    assert quality["evidence_checks"][0]["matched"] == ["return_custom"]

    missing = grade_turn(
        turn,
        {"answer": "可以退货。", "citations": []},
        200,
        [],
        empty_snapshot(),
        empty_snapshot(),
        {},
        [],
    )
    assert missing["policy_quality"]["status"] == "failed"
    assert missing["policy_quality"]["evidence_checks_passed"] == 0

    manual = Turn(
        message="保修范围是什么",
        expected_outcome="基于适用条件解释",
        checks=[],
    )
    not_scored = grade_turn(
        manual,
        {"answer": "需要先确认商品品类。"},
        200,
        [],
        empty_snapshot(),
        empty_snapshot(),
        {},
        [],
        policy_required=True,
    )
    assert not_scored["policy_quality"]["status"] == "not_scored"
    assert (
        not_scored["policy_quality"]["reason"]
        == "policy_required_without_explicit_evidence_assertion"
    )


def test_faithfulness_quality_reads_production_verdict_and_keeps_unjudged_separate():
    from eval.task_grading import faithfulness_quality

    passed = faithfulness_quality(
        {
            "events": [
                {
                    "event": "faithfulness_verdict",
                    "verdict": "pass",
                    "issues": [],
                    "evidence_refs": ["return_policy_abc123"],
                }
            ]
        }
    )
    assert passed == {
        "status": "passed",
        "source": "production_faithfulness_agent",
        "issues": [],
        "evidence_refs": ["return_policy_abc123"],
    }
    assert faithfulness_quality({})["status"] == "not_run"
    assert (
        faithfulness_quality(
            {"diagnostics": {"validation": {"status": "unavailable", "attempts": []}}}
        )["status"]
        == "unavailable"
    )


def test_quality_rollup_reports_denominators_and_excludes_unjudged_turns():
    rows = [
        {
            "id": "policy-task",
            "source_id": "source-1",
            "scenario": "policy",
            "variant": "original",
            "split": "development",
            "split_group": "g1",
            "partition_group": "ab_comparison",
            "partition_basis": "explicit_evidence_check",
            "source_file": "eval/dataset/agent_tasks.jsonl",
            "task_status": "passed",
            "deterministic_status": "passed",
            "turns": [
                {
                    "policy_quality": {
                        "status": "passed",
                        "evidence_checks": [{"passed": True}],
                    },
                    "faithfulness_quality": {"status": "passed"},
                },
                {
                    "policy_quality": {"status": "not_applicable", "evidence_checks": []},
                    "faithfulness_quality": {"status": "not_run"},
                },
            ],
        }
    ]
    quality = quality_summary(rows)
    assert quality["policy"]["evidence_check_count"] == 1
    assert quality["policy"]["evidence_check_coverage"] == 1.0
    assert quality["faithfulness"]["judged_turns"] == 1
    assert quality["faithfulness"]["pass_rate_among_judged"] == 1.0
    assert quality["faithfulness"]["not_run"] == 1

    report = summarize(rows)
    assert report["overall"]["quality"] == quality
    assert report["by_partition_group"]["ab_comparison"]["n"] == 1
    assert report["by_source_file"]["eval/dataset/agent_tasks.jsonl"]["n"] == 1


@pytest.mark.asyncio
async def test_multiturn_refund_uses_api_and_commits_exactly_expected_changes():
    async def factory(settings):
        c = await build_container(settings)
        c.chat_service.runner.llm = type(
            "Scripted",
            (),
            {
                "generate": AsyncMock(
                    side_effect=[
                        LLMResponse("请提供订单号和退款原因。"),
                        LLMResponse(
                            "",
                            tool_calls=[
                                {
                                    "id": "refund-call",
                                    "type": "function",
                                    "function": {
                                        "name": "create_refund_request",
                                        "arguments": json.dumps(
                                            {"order_id": "ORD10002", "reason": "收到商品有裂纹"}
                                        ),
                                    },
                                }
                            ],
                            finish_reason="tool_calls",
                        ),
                    ]
                )
            },
        )()
        return c

    report = await evaluate(task_ids=["agent_032_refund"], container_factory=factory)
    case = report["cases"][0]
    assert case["task_status"] == "passed"
    assert len(case["turns"]) == 3
    assert case["turns"][2]["request"]["session_id"] == case["turns"][0]["response"]["session_id"]
    changes = case["turns"][2]["business_deltas"]
    assert {d["table"] for d in changes} == {"refunds", "orders", "audit_logs"}
    confirmed = [
        call for call in case["turns"][2]["calls"] if call["name"] == "create_refund_request"
    ]
    assert len(confirmed) == 1 and confirmed[0]["confirmed"]
    assert report["by_source_family"]["i032"]["member_ids"] == ["agent_032_refund"]
    assert not report["by_source_family"]["i032"]["complete"]
    assert report["by_split"]["holdout"]["n"] == 0


@pytest.mark.asyncio
async def test_wrong_proposal_is_not_confirmed_by_evaluator():
    async def factory(settings):
        c = await build_container(settings)
        c.chat_service.runner.llm = type(
            "Scripted",
            (),
            {
                "generate": AsyncMock(
                    side_effect=[
                        LLMResponse("请提供订单号和原因"),
                        LLMResponse(
                            "",
                            tool_calls=[
                                {
                                    "id": "wrong",
                                    "type": "function",
                                    "function": {
                                        "name": "create_refund_request",
                                        "arguments": json.dumps(
                                            {"order_id": "ORD10001", "reason": "收到商品有裂纹"}
                                        ),
                                    },
                                }
                            ],
                            finish_reason="tool_calls",
                        ),
                    ]
                )
            },
        )()
        return c

    report = await evaluate(task_ids=["agent_032_refund"], container_factory=factory)
    case = report["cases"][0]
    assert case["task_status"] == "failed"
    assert case["turns"][2]["status"] == "blocked"
    assert not any(c["confirmed"] for t in case["turns"] for c in t.get("calls", []))
    assert not any(t.get("business_deltas") for t in case["turns"])


def synthetic_review_case(deterministic="passed"):
    task = next(t for t in load_tasks() if t.id == "agent_012")
    return {
        "id": task.id,
        "source_id": task.source_id,
        "variant": task.variant,
        "scenario": task.scenario,
        "split": task.split,
        "derived_from": [],
        "deterministic_status": deterministic,
        "semantic_status": "pending",
        "task_status": "review_required" if deterministic == "passed" else "failed",
        "review_criterion": task.semantic_review,
        "turns": [
            {
                "request": {"message": "积分怎么用"},
                "response": {"answer": "100分抵1元"},
                "http_status": 200,
            }
        ],
    }


def test_semantic_review_is_response_bound_and_cannot_override_invariants():
    for deterministic in ["passed", "failed"]:
        case = synthetic_review_case(deterministic)
        report = {"corpus_digest": corpus_digest(), "cases": [case]}
        labels = {
            "corpus_digest": corpus_digest(),
            "reviews": [
                {
                    "task_id": case["id"],
                    "response_digest": response_digest(case),
                    "decision": "pass",
                    "reviewer": "reviewer-test",
                    "rationale": "核对原文，测试标签非实际人工审核",
                }
            ],
        }
        result = apply_reviews(report, labels)
        assert result["cases"][0]["task_status"] == (
            "passed" if deterministic == "passed" else "failed"
        )
        wrong = copy.deepcopy(labels)
        wrong["reviews"][0]["response_digest"] = "old-run"
        with pytest.raises(ValueError, match="different response"):
            apply_reviews(report, wrong)


def test_side_effect_checker_detects_duplicate_applications_and_hidden_proposals():
    from eval.task_fixtures import validate_changes
    from eval.task_schema import ChangeRule

    before = empty_snapshot()
    after = copy.deepcopy(before)
    after["returns"] = [
        {"return_id": "one", "order_id": "ORD10002", "status": "requested"},
        {"return_id": "two", "order_id": "ORD10002", "status": "requested"},
    ]
    failures, _ = validate_changes(
        before,
        after,
        [
            ChangeRule(
                table="returns",
                kind="insert",
                where={"order_id": "ORD10002", "status": "requested"},
            )
        ],
    )
    assert any("expected 1, observed 2" in f for f in failures)
    after = copy.deepcopy(before)
    after["harness_actions"] = [{"action_id": "hidden", "status": "pending"}]
    turn = Turn(
        message="你好",
        expected_outcome="直接回答",
        checks=[Check(kind="no_proposal", note="不能暗中生成提案")],
    )
    result = grade_turn(turn, {"answer": "你好"}, 200, [], before, after, {}, [])
    assert any("no_proposal" in f for f in result["failures"])


@pytest.mark.asyncio
async def test_api_error_is_reported_and_does_not_drop_remaining_cases():
    async def factory(settings):
        c = await build_container(settings)
        c.chat_service.runner.llm = type(
            "Scripted",
            (),
            {
                "generate": AsyncMock(
                    side_effect=[RuntimeError("synthetic transport failure"), LLMResponse("你好")]
                )
            },
        )()
        return c

    report = await evaluate(task_ids=["agent_001", "agent_003"], container_factory=factory)
    assert report["overall"]["n"] == 2
    assert report["cases"][0]["task_status"] == "error"
    assert report["cases"][0]["deterministic_status"] == "not_evaluated"
    assert report["cases"][1]["task_status"] == "passed"
    assert report["by_split"]["holdout"]["n"] == 0


def test_result_acceptance_allows_search_or_direct_lookup_without_fixed_path():
    turn = Turn(
        message="T恤是纯棉吗",
        expected_outcome="依据目录回答",
        checks=[Check(kind="target_observed", value={"sku": "SKU-TEE"}, note="须核实正确商品")],
    )
    state = empty_snapshot()
    for name, args, output in [
        (
            "search_products",
            {"query": "T恤"},
            {"products": [{"sku": "SKU-TEE", "name": "纯棉T恤"}]},
        ),
        ("get_product_detail", {"sku": "SKU-TEE"}, {"sku": "SKU-TEE", "name": "纯棉T恤"}),
    ]:
        call = {
            "name": name,
            "arguments": args,
            "user_id": "u_demo",
            "executed": True,
            "confirmed": False,
            "success": True,
            "result": {"success": True, "output": output},
        }
        grade = grade_turn(turn, {"answer": "这款是纯棉T恤。"}, 200, [call], state, state, {}, [])
        assert not grade["failures"]


def test_english_greeting_keyword_is_not_a_substring_of_shipping():
    from eval.task_grading import has_phrase

    assert not has_phrase("Shipping policy", "hi")
    assert has_phrase("Hi! How can I help?", "hi")


def test_policy_answer_or_necessary_clarification_are_both_accepted_paths():
    task = next(t for t in load_tasks() if t.id == "agent_018")
    state = empty_snapshot()
    result = grade_turn(
        task.turns[0],
        {"answer": "请问是哪类商品，需要具体品类才能确认保修期限。"},
        200,
        [],
        state,
        state,
        {},
        [],
    )
    assert not result["failures"]
    assert result["accepted_paths"] == ["先补问确实缺失的品类或退款定位条件"]
    unsupported = grade_turn(
        task.turns[0], {"answer": "所有商品都免费保修12个月"}, 200, [], state, state, {}, []
    )
    assert unsupported["failures"]


def test_handoff_requires_a_real_result_or_an_honest_capability_boundary():
    task = next(t for t in load_tasks() if t.id == "agent_066")
    state = empty_snapshot()
    generic = grade_turn(
        task.turns[0], {"answer": "人工客服9点上班，请排队。"}, 200, [], state, state, {}, []
    )
    assert generic["failures"]
    call = {
        "name": "any_authorized_handoff",
        "arguments": {"reason": "用户要求人工"},
        "user_id": "u_demo",
        "executed": True,
        "confirmed": False,
        "success": True,
        "result": {"output": {"queue": "human", "ticket_id": "ticket-eval"}},
    }
    real = grade_turn(
        task.turns[0], {"answer": "已进入人工队列，尚未接通。"}, 200, [call], state, state, {}, []
    )
    assert not real["failures"]


def test_critic_quality_keeps_legacy_interface_without_claiming_fact_verification():
    from eval.task_grading import faithfulness_quality

    for verdict, expected in [("pass", "passed"), ("revise", "failed")]:
        result = faithfulness_quality(
            {
                "events": [
                    {
                        "event": "faithfulness_verdict",
                        "agent": "critic",
                        "review_type": "critic",
                        "verdict": verdict,
                        "evidence_checked": False,
                    }
                ]
            }
        )
        assert result["status"] == expected
        assert result["source"] == "production_critic_agent"
        assert result["review_type"] == "critic"
        assert result["facts_verified"] is False
        assert result["evidence_checked"] is False


def test_failed_critic_recheck_is_not_reported_as_previous_verdict():
    from eval.task_grading import faithfulness_quality

    result = faithfulness_quality(
        {
            "events": [
                {"event": "faithfulness_verdict", "review_type": "critic", "verdict": "revise"}
            ],
            "diagnostics": {"validation": {"status": "unavailable", "review_type": "critic"}},
        }
    )
    assert result["status"] == "unavailable"
    assert result["facts_verified"] is False


def test_deterministic_output_rejection_is_not_misattributed_to_critic():
    from eval.task_grading import faithfulness_quality

    result = faithfulness_quality(
        {
            "diagnostics": {
                "validation": {
                    "status": "rejected",
                    "attempts": [
                        {
                            "stage": "deterministic",
                            "reason": "unknown_citation_or_action_claim",
                        }
                    ],
                }
            }
        }
    )
    assert result["status"] == "not_run"
    assert result["source"] == "deterministic_output_gate"
    assert result["deterministic_gate_status"] == "rejected"


def test_nonstreaming_critic_diagnostics_preserve_latest_issues():
    from eval.task_grading import faithfulness_quality

    result = faithfulness_quality(
        {
            "diagnostics": {
                "validation": {
                    "review_type": "critic",
                    "status": "revise",
                    "evidence_checked": True,
                    "attempts": [
                        {
                            "stage": "critic",
                            "verdict": {
                                "verdict": "revise",
                                "issues": ["当前答复遗漏第二问"],
                                "evidence_refs": ["get_order"],
                            },
                        }
                    ],
                }
            }
        }
    )
    assert result["status"] == "failed"
    assert result["issues"] == ["当前答复遗漏第二问"]
    assert result["evidence_checked"] is True and result["facts_verified"] is False
