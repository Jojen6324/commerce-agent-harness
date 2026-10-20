from types import SimpleNamespace

from app.harness.delegation_policy import DelegationMode, DelegationPolicy


def test_required_policy_expert_runs_for_rag_but_orders_remains_optional():
    policy = DelegationPolicy(DelegationMode.REQUIRED_WHEN_ELIGIBLE)

    order = policy.decide("核实 ORD10001 的物流状态")
    cross_domain = policy.decide("核实 ORD10001 是否签收，以及七天无理由从哪天起算")
    adjacent_order_id = policy.decide("ORD10002买的是哪种杯子？现在不喜欢图案能无理由退吗？")
    simple = policy.decide("你好")

    assert order.experts == ()
    assert cross_domain.experts == ("policy",)
    assert adjacent_order_id.experts == ("policy",)
    assert simple.experts == ()


def test_policy_uses_history_and_confirmed_facts_for_anaphoric_follow_up():
    policy = DelegationPolicy("required_when_eligible")
    decision = policy.decide(
        "那这个还能退吗？",
        history=[SimpleNamespace(role="user", content="我买了 ORD10001，帮我看下商品信息")],
        confirmed_facts={"last_order": {"order_id": "ORD10001"}},
    )

    assert decision.experts == ("policy",)


def test_auto_only_selects_independent_cross_domain_work_and_off_selects_none():
    query = "查 ORD10001 的签收状态，并核实七天无理由期限从哪天起算"
    assert DelegationPolicy("auto").decide(query).experts == ("policy", "orders")
    assert DelegationPolicy("auto").decide("查 ORD10001 的物流").experts == ()
    assert DelegationPolicy("off").decide(query).experts == ()


def test_required_policy_uses_live_retrieval_need_not_order_identity_or_dataset_labels():
    policy = DelegationPolicy("required_when_eligible")
    assert policy.decide("核实 ORD778899 的物流状态").experts == ()
    assert policy.decide("退款政策是什么").experts == ("policy",)
    assert policy.decide("忘记订单号了，能不能退款").experts == ("policy",)
    assert policy.decide("ORD778899退款进度如何").experts == ()
    assert policy.decide("核实 ORD778899 的商品，破损的退货政策怎么规定").experts == ("policy",)


def test_expert_context_remains_valid_json_when_history_and_facts_are_large():
    import json

    context = DelegationPolicy.context_for_expert(
        "查订单政策",
        [{"role": "user", "content": "历史" * 5000}],
        {"business": {"large": "事实" * 10000}, "last_order": {"order_id": "ORD778899"}},
    )
    decoded = json.loads(context)
    assert len(context) <= 3900
    assert decoded["omitted_context"] is True
    assert decoded["confirmed_facts"]["last_order"]["order_id"] == "ORD778899"


def test_expert_goals_are_subtasks_instead_of_copies_of_parent_question():
    query = "核实ORD778899是否签收，并说明七天无理由从哪日起算"
    order_goal = DelegationPolicy.goal_for_expert("orders", query)
    policy_goal = DelegationPolicy.goal_for_expert("policy", query)
    assert "ORD778899" in order_goal and "七天无理由" not in order_goal
    assert "七天无理由" in policy_goal and "ORD778899" not in policy_goal
    assert query not in order_goal and query not in policy_goal
