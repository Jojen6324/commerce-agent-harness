"""User/session scoped facts, artifacts and run records. No credentials stored."""

import json

from sqlalchemy import select

from app.core.exceptions import InvalidRequestError
from app.core.ids import new_id
from app.repositories.models import ActionRow, ArtifactRow, RunRow, RuntimeStateRow


class RuntimeStore:
    def __init__(self, factory):
        self.factory = factory

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
                )
            )
            facts["pending_actions"] = [json.loads(a.proposal_json) for a in pending]
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

    async def put(self, data, context):
        ref = new_id("art_")
        async with self.factory() as db:
            db.add(
                ArtifactRow(
                    ref=ref,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    content_json=json.dumps(data, ensure_ascii=False),
                )
            )
            await db.commit()
        return ref

    async def read(self, ref, context):
        async with self.factory() as db:
            row = await db.get(ArtifactRow, ref)
            if not row or row.user_id != context.user_id or row.session_id != context.session_id:
                raise InvalidRequestError("artifact not found")
            return json.loads(row.content_json)

    async def record(self, result, context, parent_run_id=None):
        async with self.factory() as db:
            row = await db.get(RunRow, result["run_id"])
            if row is None:
                row = RunRow(
                    run_id=result["run_id"],
                    parent_run_id=parent_run_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                )
                db.add(row)
            if parent_run_id is not None:
                row.parent_run_id = parent_run_id
            row.status = result["status"]
            row.events_json = json.dumps(result["events"], ensure_ascii=False)
            await db.commit()
