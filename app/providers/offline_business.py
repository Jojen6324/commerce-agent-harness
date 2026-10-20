"""Deterministic test provider decisions; never used by live models."""

import json
import re

from app.core.ids import new_id


def offline_decision(query, observations, history, *, spec=None):
    if observations:
        result = observations[-1]
        out = result["output"]
        data = out.get("output", out)
        if data.get("answer"):
            return [], data["answer"]
        if not out.get("success", True):
            return [], "查询失败，请核对信息或联系人工客服。"
        if result["name"] == "get_order":
            statuses = {
                "paid": "已支付",
                "shipped": "已发货",
                "delivered": "已签收",
                "cancelled": "已取消",
            }
            return (
                [],
                f"订单 {data.get('order_id')} 当前状态：{statuses.get(data.get('status'),data.get('status'))}。",
            )
        return [], "查询结果：" + json.dumps(data, ensure_ascii=False)
    if query.strip() in ("你好", "您好", "hi", "hello"):
        return [], "你好，我可以帮你查看商品政策、订单、物流和售后问题。"
    order = re.search(r"ORD[\w-]+", query, re.I)
    if not order:
        for m in reversed(history):
            order = re.search(r"ORD[\w-]+", m.content, re.I)
            if order:
                break
    if any(w in query for w in ("订单", "物流", "取消", "退款申请", "退货申请")):
        if not order:
            return [
                {
                    "id": new_id("call_"),
                    "type": "function",
                    "function": {
                        "name": "ask_user",
                        "arguments": json.dumps(
                            {"question": "请提供要处理的订单号。", "missing_fields": ["order_id"]},
                            ensure_ascii=False,
                        ),
                    },
                }
            ], ""
        name = (
            "cancel_order"
            if "取消" in query
            else "track_shipment"
            if "物流" in query
            else "get_order"
        )
        args = {"order_id": order.group().upper()}
    else:
        name, args = (
            "search_knowledge",
            {"query": query, "complex": any(w in query for w in ("但", "定制", "同时"))},
        )
    return [
        {
            "id": new_id("call_"),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
    ], ""
