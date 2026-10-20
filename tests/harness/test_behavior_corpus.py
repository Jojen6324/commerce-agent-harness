import sys

from app.providers.offline_business import offline_decision
from app.schemas.chat import ChatMessage
from eval.behavior import cases


def test_original_intent_corpus_becomes_behavior_cases():
    rows = cases()
    assert len(rows) == 116
    assert sum(r["variant"] == "original" for r in rows) == 100
    assert all("source_id" in r and "expected_outcome" in r for r in rows)
    assert all("acceptable_tools" not in r for r in rows)
    assert "app.agents.graph" not in sys.modules
    assert "app.agents.nodes.intent" not in sys.modules


def test_offline_greeting_does_not_hide_cancellation_and_multiturn_order():
    calls, _ = offline_decision("你好，我想取消订单 ORD10003", [], [])
    assert calls[0]["function"]["name"] == "cancel_order"
    calls, _ = offline_decision(
        "这笔订单的物流呢", [], [ChatMessage(role="user", content="订单 ORD10002")]
    )
    assert calls[0]["function"]["name"] == "track_shipment"
    assert "ORD10002" in calls[0]["function"]["arguments"]
