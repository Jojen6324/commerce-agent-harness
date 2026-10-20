"""Project server-confirmed purchase facts without reviving stale live status."""

from app.harness.requirements import ANAPHORA, ORDER_ID


def confirmed_item_observations(facts, request, user_id):
    """Read existing user/session-scoped facts, never model-authored summaries.

    Only immutable purchased item lines are reusable across turns. Live order,
    shipment and refund states are deliberately excluded and must be refreshed.
    """
    targets = set(ORDER_ID.findall(request))
    if not targets and ANAPHORA.search(request):
        last = facts.get("last_order") or {}
        if last.get("order_id"):
            targets.add(last["order_id"])
    observations = []
    for key, fact in (facts.get("business") or {}).items():
        if not isinstance(fact, dict) or fact.get("tool") not in {"get_order", "get_order_items"}:
            continue
        result = fact.get("result") or {}
        order_id = result.get("order_id") or key.split(":", 1)[0]
        if order_id not in targets or result.get("user_id", user_id) != user_id:
            continue
        if not result.get("items"):
            continue
        observations.append(
            {
                "name": fact["tool"],
                "arguments": {"order_id": order_id},
                "output": {
                    "success": True,
                    "output": {"order_id": order_id, "items": result["items"]},
                },
                "source": "confirmed_session_fact",
                "snapshot_fields_only": True,
            }
        )
    return observations
