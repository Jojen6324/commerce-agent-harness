"""User/session scoped facts, artifacts and run records. No credentials stored."""

import json

from app.core.exceptions import InvalidRequestError
from app.core.ids import new_id
from app.repositories.models import ArtifactRow, RunRow, RuntimeStateRow


class RuntimeStore:
    def __init__(self, factory):
        self.factory = factory

    async def facts(self, context):
        async with self.factory() as db:
            row = await db.get(RuntimeStateRow, context.session_id)
            if row and row.user_id != context.user_id:
                raise InvalidRequestError("session not found")
            return json.loads(row.facts_json) if row else {}

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
            db.add(
                RunRow(
                    run_id=result["run_id"],
                    parent_run_id=parent_run_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    status=result["status"],
                    events_json=json.dumps(result["events"], ensure_ascii=False),
                )
            )
            await db.commit()
