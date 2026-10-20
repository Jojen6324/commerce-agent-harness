"""Minimal server-owned case state; model summaries cannot overwrite authority."""

import copy
import re
import time

from app.core.ids import new_id
from app.harness.requirements import request_requirements

LIVE_TOOLS = {
    "get_order",
    "get_order_items",
    "track_shipment",
    "get_refund_status",
    "check_inventory",
    "delegate_task",
}


class BusinessCases:
    def __init__(self, ttl=30):
        self.ttl = ttl

    def prepare(self, facts, message):
        facts = copy.deepcopy(facts)
        facts.pop("_case_cancelled", None)
        case = facts.get("active_case")
        explicit_targets = re.findall(r"(?<![A-Za-z0-9_])ORD[A-Za-z0-9_-]+", message, re.I)
        cancelled = bool(
            re.search(
                r"取消(?:当前|这个|本次)(?:任务|咨询)|不(?:用|要)继续(?:处理|办理)|不办了|不退了|撤销(?:本次)?(?:方案|申请)",
                message,
            )
        )
        different = bool(
            case
            and explicit_targets
            and case.get("target")
            and case["target"] not in explicit_targets
        )
        if cancelled:
            facts["_case_cancelled"] = True
        if case and (cancelled or different):
            case["stage"] = "cancelled" if cancelled else "superseded"
            facts.setdefault("case_history", []).append(case)
            facts["case_history"] = facts["case_history"][-8:]
            case = None
        if not case or case.get("stage") in {"completed", "cancelled", "superseded"}:
            case = {
                "case_id": new_id("case_"),
                "business_type": "general",
                "owner_role": "main",
                "goal": message,
                "target": explicit_targets[0] if explicit_targets else None,
                "target_order_id": explicit_targets[0] if explicit_targets else None,
                "target_product_sku": None,
                "tasks": [],
                "stage": "open",
                "missing_fields": [],
                "pending_action_id": None,
                "observations": [],
                "updated_at": time.time(),
            }
        requirements = request_requirements(message)
        case["proposals_forbidden"] = requirements.proposals_forbidden or (
            case.get("proposals_forbidden", False) and not requirements.action_requested
        )
        case["operation_authorized"] = not case["proposals_forbidden"] and (
            requirements.action_requested or case.get("operation_authorized", False)
        )
        facts["active_case"] = case
        return facts

    def model_facts(self, facts):
        # Do not copy complete tool payloads or stale real-time state into prompts.
        case = copy.deepcopy(facts.get("active_case", {}))
        now = time.time()
        case["observations"] = [
            o
            for o in case.get("observations", [])
            if o["tool"] not in LIVE_TOOLS or now - o["observed_at"] <= self.ttl
        ]
        return {
            "active_case": case,
            "last_order": facts.get("last_order"),
            "pending_actions": facts.get("pending_actions", []),
            "evidence_index": facts.get("evidence_refs", [])[-12:],
        }

    def update(self, facts, message, result):
        case = facts["active_case"]
        refs = list(case.get("observations", []))
        for o in result.get("observations", []):
            name = o.get("name")
            out = o.get("output", {})
            if out.get("success") is False:
                continue
            if name == "delegate_task":
                role = o.get("arguments", {}).get("agent")
                if role in {"shopping", "order", "aftersales"}:
                    if case["business_type"] not in {"general", role}:
                        case["previous_goal"] = case["goal"]
                        case["goal"] = message
                    case.update(owner_role=role, business_type=role)
                    case.setdefault("tasks", []).append(
                        {
                            "role": role,
                            "goal": o["arguments"].get("goal", "")[:1000],
                            "status": out.get("status"),
                            "child_run_id": out.get("child_run_id"),
                            "missing_fields": out.get("missing_fields", []),
                        }
                    )
                    case["tasks"] = case["tasks"][-6:]
            elif name in {"create_return_request", "create_refund_request", "get_refund_status"}:
                case.update(business_type="aftersales", owner_role=o.get("delegated_by", "main"))
            elif (
                name in {"get_order", "track_shipment", "cancel_order"}
                and case["business_type"] == "general"
            ):
                case["business_type"] = "order"
            elif (
                name in {"search_products", "get_product_detail"}
                and case["business_type"] == "general"
            ):
                case["business_type"] = "shopping"
            args = o.get("arguments", {})
            if args.get("order_id"):
                case["target_order_id"] = args["order_id"]
                case["target"] = args["order_id"]
            if args.get("sku"):
                case["target_product_sku"] = args["sku"]
                if not case.get("target_order_id"):
                    case["target"] = args["sku"]
            if name == "ask_user":
                case["missing_fields"] = out.get("missing_fields", [])
            if o.get("artifact_ref"):
                refs.append(
                    {
                        "tool": name,
                        "artifact_ref": o["artifact_ref"],
                        "observed_at": o.get("observed_at", time.time()),
                    }
                )
        case["observations"] = refs[-12:]
        status = result.get("status")
        if facts.pop("_case_cancelled", False):
            status = "cancelled"
        case["stage"] = {
            "needs_input": "needs_input",
            "awaiting_confirmation": "pending_confirmation",
            "completed": "completed",
            "cancelled": "cancelled",
        }.get(status, "failed")
        if status != "needs_input":
            case["missing_fields"] = []
        pending = result.get("pending_action") or {}
        case["pending_action_id"] = pending.get("action_id")
        case["updated_at"] = time.time()
        return facts
