"""Behavior evaluation over the retained intent corpus; labels are offline metadata."""

import json
from pathlib import Path

from app.config.settings import Settings
from app.schemas.chat import ChatRequest
from app.services.container import build_container

# Multiple acceptable tools/paths; no unique sequence or runtime classifier.
CAPABILITIES = {
    "greeting": [],
    "chitchat": [],
    "out_of_scope": [],
    "faq_policy": ["search_knowledge", "answer_question", "delegate_task"],
    "return_exchange": [
        "search_knowledge",
        "answer_question",
        "get_order",
        "get_order_items",
        "create_return_request",
        "delegate_task",
    ],
    "refund": [
        "get_refund_status",
        "create_refund_request",
        "answer_question",
        "search_knowledge",
        "delegate_task",
    ],
    "cancel_order": ["cancel_order", "get_order", "answer_question", "search_knowledge"],
    "order_inquiry": ["get_order", "get_order_items", "delegate_task"],
    "logistics": ["track_shipment", "get_order", "delegate_task"],
    "product_consult": [
        "get_product_detail",
        "search_products",
        "check_inventory",
        "answer_question",
        "delegate_task",
    ],
    "recommendation": ["recommend_products", "search_products", "delegate_task"],
    "human_handoff": ["escalate_to_human"],
}


def cases():
    path = Path(__file__).parent / "dataset/intent_100.jsonl"
    return [
        {
            **r,
            "acceptable_tools": CAPABILITIES.get(
                r["intent"], ["answer_question", "search_knowledge"]
            ),
        }
        for line in path.read_text().splitlines()
        if line.strip()
        for r in [json.loads(line)]
    ]


async def evaluate(*, live=False, limit=10):
    settings = Settings(
        database_url="sqlite+aiosqlite:///./data/local/behavior-eval.db",
        app_env="development" if live else "testing",
        rate_limit_per_minute=max(60, limit * 2),
    )
    if live and not settings.agnes.get_secret_value():
        raise RuntimeError("AGNES binding required")
    c = await build_container(settings, seed=True)
    details = []
    try:
        for row in cases()[:limit]:
            result = await c.chat_service.chat(
                ChatRequest(message=row["query"], user_id="u_demo"),
                trace_id="behavior_" + row["id"],
            )
            tools = result.diagnostics.get("tools", [])
            acceptable = row["acceptable_tools"]
            # Clarifying information is a legitimate path without a tool call.
            clarify = not tools and any(
                t in result.answer for t in ("请提供", "请补充", "订单号", "需要您提供")
            )
            ok = bool(set(tools) & set(acceptable)) or (not tools and (not acceptable or clarify))
            details.append(
                {
                    "id": row["id"],
                    "query": row["query"],
                    "tools": tools,
                    "clarified": clarify,
                    "capability_match": ok,
                    "status": result.status,
                    "model_calls": result.diagnostics.get("model_calls", 0),
                    "tokens": result.token_usage.total_tokens,
                    "latency_ms": result.latency.total_ms,
                }
            )
    finally:
        await c.aclose()
    return {
        "mode": "real_model" if live else "deterministic_offline",
        "n": len(details),
        "capability_match_rate": sum(r["capability_match"] for r in details) / len(details)
        if details
        else None,
        "scope": "Tool capability/clarification regression; not semantic task success or intent accuracy.",
        "cases": details,
    }
