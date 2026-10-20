"""Request-time evidence routing for the C-mode delegation path.

This policy only sees the live conversation and user-scoped confirmed facts. It
has no dependency on benchmark IDs, scenarios, labels, or expected answers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.prompts.delegation import ORDER_TASK, POLICY_TASK


class DelegationMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED_WHEN_ELIGIBLE = "required_when_eligible"


@dataclass(frozen=True)
class DelegationDecision:
    mode: DelegationMode
    experts: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return bool(self.experts)


class DelegationPolicy:
    """Select read-only evidence experts from the active conversational need."""

    _ORDER_TERMS = (
        "订单",
        "订单号",
        "订单状态",
        "物流",
        "快递",
        "运单",
        "发货",
        "签收",
        "退款进度",
        "退货进度",
        "我买的",
        "我这单",
        "订单里的",
        "购买记录",
    )
    _KNOWLEDGE_TERMS = (
        "政策",
        "规则",
        "规定",
        "能不能",
        "能否",
        "可不可以",
        "可以吗",
        "是否可以",
        "符合",
        "资格",
        "退货",
        "退款",
        "换货",
        "保修",
        "质保",
        "费用",
        "时效",
        "期限",
        "七天无理由",
        "无理由",
        "起算",
        "从哪天",
        "从哪一天",
        "按哪天",
        "退吗",
        "能退",
        "可退",
        "优惠券",
        "发票",
        "赔偿",
        "售后",
        "如何办理",
        "怎么办",
        "怎么处理",
    )
    _ANAPHORA = re.compile(r"(?:这个|那个|它|这单|那单|刚才|上面|之前|同一笔|该订单)")
    _ORDER_ID = re.compile(r"(?<![A-Za-z0-9_])ORD[A-Za-z0-9_-]+(?![A-Za-z0-9_])", re.IGNORECASE)

    def __init__(self, mode: DelegationMode | str = DelegationMode.AUTO):
        self.mode = DelegationMode(mode)

    def decide(
        self,
        request: str,
        *,
        history: list[Any] | None = None,
        confirmed_facts: dict[str, Any] | None = None,
    ) -> DelegationDecision:
        if self.mode is DelegationMode.OFF:
            return DelegationDecision(self.mode)

        facts = confirmed_facts or {}
        recent_user_text = []
        for item in (history or [])[-8:]:
            role = getattr(item, "role", None)
            content = getattr(item, "content", "")
            if isinstance(item, dict):
                role, content = item.get("role"), item.get("content", "")
            if role == "user" and isinstance(content, str):
                recent_user_text.append(content)

        # Old requests are only context for a short anaphoric follow-up. This
        # prevents an earlier unrelated topic from forcing a specialist call.
        text = request
        if self._ANAPHORA.search(request) and recent_user_text:
            text = "\n".join([*recent_user_text[-2:], request])

        has_order_facts = self._has_order_facts(facts)
        asks_order = bool(self._ORDER_ID.search(text)) or any(
            term in text for term in self._ORDER_TERMS
        )
        asks_policy = any(term in text for term in self._KNOWLEDGE_TERMS)

        # A policy question about an already identified purchase needs both the
        # policy source and the order/item facts that determine applicability.
        if asks_policy and has_order_facts and self._ANAPHORA.search(request):
            asks_order = True

        if self.mode is DelegationMode.AUTO:
            experts = ("knowledge", "orders") if asks_order and asks_policy else ()
            return DelegationDecision(
                self.mode,
                experts,
                tuple(f"{role}:request_or_confirmed_context" for role in experts),
            )

        # Delegation needs distinct investigations, not merely an order keyword.
        # Missing identity is clarified by the main agent before expert work.
        identified_order = bool(self._ORDER_ID.search(text)) or has_order_facts
        policy_question = any(
            term in request
            for term in (
                "政策",
                "规则",
                "能否",
                "能不能",
                "可否",
                "能退",
                "退吗",
                "无理由",
                "如何",
                "怎么",
                "流程",
                "条件",
                "允许",
                "起算",
                "从哪",
                "叠加",
                "区别",
                "保修",
                "质量",
                "裂纹",
                "赔偿",
                "取消",
                "售后",
                "假如",
                "洗过",
            )
        )
        status_only = re.search(r"(?:退款|退货|物流|订单)(?:状态|进度)", request) and not any(
            term in request
            for term in ("政策", "规则", "区别", "条件", "流程", "期限", "赔偿", "能否", "能不能")
        )
        if status_only:
            policy_question = False
        eligible = (
            ("orders", "knowledge")
            if (asks_order and asks_policy and policy_question and identified_order)
            else ()
        )
        reasons = tuple(f"{role}:request_or_confirmed_context" for role in eligible)
        return DelegationDecision(self.mode, eligible, reasons)

    @staticmethod
    def goal_for_expert(expert: str, request: str) -> str:
        if expert == "orders":
            identifiers = DelegationPolicy._ORDER_ID.findall(request)
            target = "、".join(identifiers) or "上下文中已确认的订单"
            return ORDER_TASK.format(target=target)
        clauses = re.split(r"[，,；;。？?]|并且|以及|同时|然后|并|再", request)
        policy_clauses = [
            clause
            for clause in clauses
            if any(term in clause for term in DelegationPolicy._KNOWLEDGE_TERMS)
        ]
        topic = DelegationPolicy._ORDER_ID.sub("该订单", "；".join(policy_clauses) or request)
        return POLICY_TASK.format(topic=topic[:1400])

    @staticmethod
    def _has_order_facts(facts: dict[str, Any]) -> bool:
        if facts.get("last_order"):
            return True
        business = facts.get("business")
        if isinstance(business, dict) and any(
            isinstance(item, dict)
            and item.get("tool")
            in {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
            for item in business.values()
        ):
            return True
        return False

    @staticmethod
    def context_for_expert(request: str, history: list[Any] | None, facts: dict[str, Any]) -> str:
        """Build bounded, data-only context for DelegateInput.context."""
        prior = []
        for item in (history or [])[-6:]:
            role = getattr(item, "role", None)
            content = getattr(item, "content", "")
            if isinstance(item, dict):
                role, content = item.get("role"), item.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                prior.append({"role": role, "content": content[-500:]})
        # Keep valid structured data, never a character-truncated JSON prefix.
        scoped_facts = dict(facts)
        observations = scoped_facts.pop("verified_observations", [])
        observations = [o for o in observations if o.get("output", {}).get("success")]
        data = {
            "user_request": request[:1600],
            "recent_history": prior,
            "confirmed_facts": scoped_facts,
            "verified_observations": observations,
            "omitted_context": False,
        }

        def encode():
            return json.dumps(data, ensure_ascii=False, default=str)

        while len(encode()) > 3900 and prior:
            prior.pop(0)
            data["omitted_context"] = True
        # Current-turn verified observations take priority over stored history.
        if len(encode()) > 3900:
            data["confirmed_facts"] = {"last_order": facts.get("last_order")}
            data["omitted_context"] = True
        while len(encode()) > 3900 and observations:
            observations.pop(0)
            data["omitted_context"] = True
        if len(encode()) > 3900:
            data["confirmed_facts"] = {}
        return encode()
