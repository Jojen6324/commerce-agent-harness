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

from app.harness.requirements import ORDER_ID, request_requirements
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

        needs = request_requirements(request, history=history, confirmed_facts=confirmed_facts)
        eligible = needs.order and needs.policy
        if self.mode is DelegationMode.REQUIRED_WHEN_ELIGIBLE:
            eligible = eligible and needs.identified_order
        experts = (
            (
                ("knowledge", "orders")
                if self.mode is DelegationMode.AUTO
                else ("orders", "knowledge")
            )
            if eligible
            else ()
        )
        return DelegationDecision(
            self.mode,
            experts,
            tuple(f"{role}:request_or_confirmed_context" for role in experts),
        )

    @staticmethod
    def goal_for_expert(expert: str, request: str) -> str:
        if expert == "orders":
            identifiers = ORDER_ID.findall(request)
            target = "、".join(identifiers) or "上下文中已确认的订单"
            return ORDER_TASK.format(target=target)
        clauses = re.split(r"[，,；;。？?]|并且|以及|同时|然后|并|再", request)
        policy_clauses = [clause for clause in clauses if request_requirements(clause).policy]
        topic = ORDER_ID.sub("该订单", "；".join(policy_clauses) or request)
        return POLICY_TASK.format(topic=topic[:1400])

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
