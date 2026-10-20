"""AGNES diagnostic conversations over isolated fixtures; not a benchmark score.

Exercises business consultation and continuation/confirmation. Never uses
application databases. Records each turn before proceeding to the next one.
"""

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.config.settings import Settings
from app.repositories.models import ReturnRow
from app.schemas.chat import ChatRequest
from app.services.container import build_container
from eval.agent_ab import load_frozen_partition
from eval.task_fixtures import install_fixture, snapshot


async def debug(output, specialist_only=False):
    corpus = load_frozen_partition()
    fixture_task = next(t for t in corpus["tasks"] if t.id == "collab_003")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "diagnostic_conversations_not_benchmark",
        "provider": "agnes",
        "fixture_source": fixture_task.id,
        "generated_at": datetime.now(UTC).isoformat(),
        "conversations": [],
    }
    with tempfile.TemporaryDirectory(prefix="business-agnes-probe-") as directory:
        settings = Settings(
            app_env="development",
            database_url=f"sqlite+aiosqlite:///{directory}/probe.db",
            redis_url="",
            qdrant_url="",
            agent_delegation_policy="auto",
            agent_delegation_enabled=True,
            agent_critic_enabled=False,
        )
        if not settings.agnes.get_secret_value():
            raise RuntimeError("AGNES credential is required")
        c = await build_container(settings)

        def save():
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        async def turn(conversation, message, session_id=None, confirm_action_id=None):
            request = ChatRequest(
                message=message,
                user_id="u_demo",
                session_id=session_id,
                confirm_action_id=confirm_action_id,
            )

            async def emit(event):
                if event.get("event") in {
                    "agent_started",
                    "delegation_dispatched",
                    "tool_completed",
                    "stopped",
                }:
                    print(
                        json.dumps({"probe": conversation["id"], **event}, ensure_ascii=False),
                        flush=True,
                    )

            response = await c.chat_service.chat(
                request,
                trace_id=f"probe-{conversation['id']}-{len(conversation['turns'])+1}",
                emit=emit,
            )
            conversation["turns"].append(
                {"request": request.model_dump(), "response": response.model_dump(mode="json")}
            )
            save()
            print(
                "TURN",
                conversation["id"],
                len(conversation["turns"]),
                response.status,
                response.answer,
                flush=True,
            )
            return response

        try:
            await install_fixture(c.session_factory, fixture_task.fixtures)
            before = await snapshot(c.session_factory)
            specialist = {"id": "aftersales_consultation", "turns": []}
            report["conversations"].append(specialist)
            response = await turn(
                specialist,
                "请核实ORD10002的商品和签收情况，再核对杯子出现裂纹是否还能申请售后。只咨询，不要创建申请或转人工。",
            )
            specialist["note"] = "委派是观测指标，不是通过标准；用户不指定内部角色。"
            events = response.events
            specialist["checks"] = {
                "observed_aftersales_started": any(
                    e.get("agent") == "aftersales" and e.get("event") == "agent_started"
                    for e in events
                ),
                "observed_aftersales_order_lookup": any(
                    e.get("agent") == "aftersales"
                    and e.get("tool") in {"get_order", "get_order_items"}
                    for e in events
                ),
                "observed_aftersales_policy_lookup": any(
                    e.get("agent") == "aftersales" and e.get("tool") == "search_knowledge"
                    for e in events
                ),
                "no_order_or_policy_agent": not any(
                    e.get("agent") in {"order", "orders", "policy"} for e in events
                ),
                "no_business_side_effect": before == await snapshot(c.session_factory),
                "no_pending_action": response.pending_action is None,
            }
            save()
            if specialist_only:
                return report
            await install_fixture(c.session_factory, fixture_task.fixtures)
            continuation = {"id": "return_clarification_confirmation", "turns": []}
            report["conversations"].append(continuation)
            first = await turn(continuation, "帮我退货，订单ORD10002")
            second = await turn(continuation, "杯子有裂纹", first.session_id)
            continuation["checks"] = {
                "first_needs_input": first.status == "needs_input",
                "same_case": first.diagnostics.get("business_case", {}).get("case_id")
                == second.diagnostics.get("business_case", {}).get("case_id"),
                "second_has_proposal": second.pending_action is not None,
            }
            if second.pending_action:
                third = await turn(
                    continuation, "确认提交", first.session_id, second.pending_action.action_id
                )
                fourth = await turn(
                    continuation, "确认提交", first.session_id, second.pending_action.action_id
                )
                async with c.session_factory() as db:
                    count = await db.scalar(select(func.count()).select_from(ReturnRow))
                continuation["checks"].update(
                    one_return_record=count == 1,
                    same_confirmation_receipt=third.answer == fourth.answer,
                    receipt_does_not_claim_settlement="尚不代表退款完成" in third.answer,
                )
            save()
        finally:
            await c.aclose()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default="eval/reports/business_agnes_debug_20261020/probes.json"
    )
    parser.add_argument("--specialist-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(debug(args.output, specialist_only=args.specialist_only))
    for conversation in report["conversations"]:
        print(
            "CHECKS", conversation["id"], json.dumps(conversation.get("checks"), ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
