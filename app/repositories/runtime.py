"""User/session scoped facts, artifacts and run records. No credentials stored."""

import json
import time

from sqlalchemy import select, update

from app.core.exceptions import InvalidRequestError
from app.core.ids import new_id
from app.repositories.models import ActionRow, ArtifactRow, CheckpointRow, RunRow, RuntimeStateRow


class RuntimeStore:
    def __init__(self, factory, live_ttl=30):
        self.factory = factory
        self.live_ttl = live_ttl

    async def cancel_pending(self, context):
        async with self.factory() as db:
            await db.execute(
                update(ActionRow)
                .where(
                    ActionRow.user_id == context.user_id,
                    ActionRow.session_id == context.session_id,
                    ActionRow.status == "pending",
                )
                .values(status="cancelled")
            )
            await db.commit()

    async def save_checkpoint(self, run_id, node, context, result, budget, input_digest=""):
        # Whole node results are durable; individual business writes are never
        # retried by this runtime. Their authority stays in ActionRow.
        async with self.factory() as db:
            row = await db.get(CheckpointRow, (run_id, node))
            if row and (row.user_id != context.user_id or row.session_id != context.session_id):
                raise InvalidRequestError("checkpoint not found")
            if row is None:
                row = CheckpointRow(
                    run_id=run_id, node=node, user_id=context.user_id, session_id=context.session_id
                )
                db.add(row)
            row.payload_json = json.dumps(
                {"result": result, "budget": budget, "input_digest": input_digest},
                ensure_ascii=False,
            )
            await db.commit()

    async def load_checkpoint(self, run_id, node, context, input_digest=""):
        async with self.factory() as db:
            row = await db.get(CheckpointRow, (run_id, node))
            if not row or row.user_id != context.user_id or row.session_id != context.session_id:
                raise InvalidRequestError("checkpoint not found")
            value = json.loads(row.payload_json)
            if value.get("input_digest", "") != input_digest:
                raise InvalidRequestError("checkpoint input changed")
            pending = value["result"].get("pending_action")
            if pending:
                action = await db.get(ActionRow, pending["action_id"])
                if (
                    not action
                    or action.user_id != context.user_id
                    or action.session_id != context.session_id
                ):
                    raise InvalidRequestError("checkpoint action not found")
                if action.status != "pending" or action.expires_epoch < int(time.time()):
                    # Return the persisted real receipt, never replay a stale
                    # proposal or re-execute the node after a committed write.
                    receipt = (
                        json.loads(action.result_json)
                        if action.status in {"succeeded", "failed"}
                        else {
                            "status": "denied",
                            "answer": "待确认方案已撤销或过期，未重新执行任何操作。",
                            "observations": [],
                        }
                    )
                    value["result"] = {**value["result"], **receipt}
                    value["result"].pop("pending_action", None)
            return value

    async def facts(self, context):
        async with self.factory() as db:
            row = await db.get(RuntimeStateRow, context.session_id)
            if row and row.user_id != context.user_id:
                raise InvalidRequestError("session not found")
            facts = json.loads(row.facts_json) if row else {}
            pending = await db.scalars(
                select(ActionRow).where(
                    ActionRow.user_id == context.user_id,
                    ActionRow.session_id == context.session_id,
                    ActionRow.status == "pending",
                    ActionRow.expires_epoch >= int(time.time()),
                )
            )
            facts["pending_actions"] = [json.loads(a.proposal_json) for a in pending]
            case = facts.get("active_case") or {}
            if case.get("pending_action_id"):
                action = await db.get(ActionRow, case["pending_action_id"])
                if (
                    not action
                    or action.user_id != context.user_id
                    or action.session_id != context.session_id
                ):
                    case.update(stage="failed", pending_action_id=None)
                elif action.status != "pending" or action.expires_epoch < int(time.time()):
                    case.update(
                        stage="completed" if action.status == "succeeded" else "failed",
                        pending_action_id=None,
                    )
            return facts

    async def save_facts(self, context, facts):
        async with self.factory() as db:
            row = await db.get(RuntimeStateRow, context.session_id)
            if row and row.user_id != context.user_id:
                raise InvalidRequestError("session not found")
            if row is None:
                row = RuntimeStateRow(session_id=context.session_id, user_id=context.user_id)
                db.add(row)
            row.facts_json = json.dumps(facts, ensure_ascii=False)
            await db.commit()

    async def put(self, data, context, *, tool_name=None):
        ref = new_id("art_")
        stored = (
            {"_artifact_metadata": {"tool": tool_name, "observed_at": time.time()}, "payload": data}
            if tool_name
            else data
        )
        async with self.factory() as db:
            db.add(
                ArtifactRow(
                    ref=ref,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    content_json=json.dumps(stored, ensure_ascii=False),
                )
            )
            await db.commit()
        return ref

    async def read(self, ref, context):
        async with self.factory() as db:
            row = await db.get(ArtifactRow, ref)
            if not row or row.user_id != context.user_id or row.session_id != context.session_id:
                raise InvalidRequestError("artifact not found")
            data = json.loads(row.content_json)
            if isinstance(data, dict) and "_artifact_metadata" in data:
                metadata = data["_artifact_metadata"]
                if metadata.get("tool") in {
                    "get_order",
                    "get_order_items",
                    "track_shipment",
                    "get_refund_status",
                    "check_inventory",
                    "delegate_task",
                } and (time.time() - metadata["observed_at"] > self.live_ttl):
                    return {
                        "success": False,
                        "error_code": "STALE_OBSERVATION",
                        "observed_at": metadata["observed_at"],
                        "material_complete": False,
                        "notice": "实时业务观察已过期，请通过原业务工具刷新；旧结果仅保留在执行记录中。",
                    }
                return data["payload"]
            return data

    async def cancel_run(self, run_id, context):
        async with self.factory() as db:
            row = await db.get(RunRow, run_id)
            if row and row.user_id == context.user_id and row.session_id == context.session_id:
                events = json.loads(row.events_json)
                events.append(
                    {"event": "cancelled", "run_id": run_id, "trace_id": context.trace_id}
                )
                row.events_json = json.dumps(events, ensure_ascii=False)
                row.status = "cancelled"
                await db.commit()

    async def record(self, result, context, parent_run_id=None):
        async with self.factory() as db:
            row = await db.get(RunRow, result["run_id"])
            if parent_run_id:
                parent = await db.get(RunRow, parent_run_id)
                if (
                    not parent
                    or parent.user_id != context.user_id
                    or parent.session_id != context.session_id
                    or (parent.trace_id is not None and parent.trace_id != context.trace_id)
                ):
                    raise InvalidRequestError("run parent not found")
            if row is None:
                row = RunRow(
                    run_id=result["run_id"],
                    parent_run_id=parent_run_id,
                    trace_id=context.trace_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                )
                db.add(row)
            elif (
                row.user_id != context.user_id
                or row.session_id != context.session_id
                or (row.trace_id is not None and row.trace_id != context.trace_id)
            ):
                raise InvalidRequestError("run not found")
            if parent_run_id is not None:
                row.parent_run_id = parent_run_id
            row.trace_id = row.trace_id or context.trace_id
            row.status = result["status"]
            row.events_json = json.dumps(result["events"], ensure_ascii=False)
            await db.commit()
