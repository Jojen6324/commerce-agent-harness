from types import SimpleNamespace

from app.harness.delegation_policy import DelegationMode, DelegationPolicy


def test_required_policy_selects_only_evidence_roles_needed_by_request():
    policy = DelegationPolicy(DelegationMode.REQUIRED_WHEN_ELIGIBLE)

    order = policy.decide("核实 ORD10001 的物流状态")
    cross_domain = policy.decide("核实 ORD10001 是否签收，以及七天无理由从哪天起算")
    adjacent_order_id = policy.decide("ORD10002买的是哪种杯子？现在不喜欢图案能无理由退吗？")
    simple = policy.decide("你好")

    assert order.experts == ()
    assert cross_domain.experts == ("orders", "knowledge")
    assert adjacent_order_id.experts == ("orders", "knowledge")
    assert simple.experts == ()


def test_policy_uses_history_and_confirmed_facts_for_anaphoric_follow_up():
    policy = DelegationPolicy("required_when_eligible")
    decision = policy.decide(
        "那这个还能退吗？",
        history=[SimpleNamespace(role="user", content="我买了 ORD10001，帮我看下商品信息")],
        confirmed_facts={"last_order": {"order_id": "ORD10001"}},
    )

    assert decision.experts == ("orders", "knowledge")


def test_auto_only_selects_independent_cross_domain_work_and_off_selects_none():
    query = "查 ORD10001 的签收状态，并核实七天无理由期限从哪天起算"
    assert DelegationPolicy("auto").decide(query).experts == ("knowledge", "orders")
    assert DelegationPolicy("auto").decide("查 ORD10001 的物流").experts == ()
    assert DelegationPolicy("off").decide(query).experts == ()


def test_c_skips_simple_queries_and_missing_identity_without_dataset_labels():
    policy = DelegationPolicy("required_when_eligible")
    for query in (
        "核实 ORD778899 的物流状态",
        "退款政策是什么",
        "忘记订单号了，能不能退款",
        "ORD778899退款进度如何",
    ):
        assert policy.decide(query).experts == ()
    assert policy.decide("核实 ORD778899 的商品，破损的退货政策怎么规定").experts == (
        "orders",
        "knowledge",
    )


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
