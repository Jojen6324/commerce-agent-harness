import sys

from app.harness.runner import AgentRunner
from app.schemas.chat import ChatMessage
from eval.behavior import cases


def test_original_intent_corpus_becomes_behavior_cases():
    rows = cases()
    assert len(rows) == 100
    assert all("query" in r and "acceptable_tools" in r for r in rows)
    assert "app.agents.graph" not in sys.modules
    assert "app.agents.nodes.intent" not in sys.modules


def test_offline_greeting_does_not_hide_cancellation_and_multiturn_order():
    runner = AgentRunner(None, None, None)
    calls, _ = runner._offline("你好，我想取消订单 ORD10003", [], [])
    assert calls[0]["function"]["name"] == "cancel_order"
    calls, _ = runner._offline(
        "这笔订单的物流呢", [], [ChatMessage(role="user", content="订单 ORD10002")]
    )
    assert calls[0]["function"]["name"] == "track_shipment"
    assert "ORD10002" in calls[0]["function"]["arguments"]
