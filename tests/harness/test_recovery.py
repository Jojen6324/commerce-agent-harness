import asyncio
import time

import pytest
from sqlalchemy import func, select, update

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError
from app.repositories.models import ActionRow, ReturnRow
from app.schemas.chat import ChatRequest
from app.services.container import build_container
from app.tools.base import ToolContext


@pytest.mark.asyncio
async def test_restart_concurrent_confirmation_and_replay(tmp_path):
    settings = Settings(
        app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/recovery.db"
    )
    c = await build_container(settings, seed=True)
    request = ChatRequest(message="订单 ORD10002", user_id="u_demo")
    response = await c.chat_service.chat(request, trace_id="t")
    context = ToolContext(user_id="u_demo", session_id=response.session_id, trace_id="t")
    proposal = await c.chat_service.executor.execute(
        "create_return_request",
        {"order_id": "ORD10002", "reason": "质量问题"},
        context,
        c.chat_service.spec,
    )
    action_id = proposal["pending_action"]["action_id"]
    await c.aclose()
    c = await build_container(settings)
    try:
        confirmation = ChatRequest(
            message="确认，但不要改参数",
            user_id="u_demo",
            session_id=response.session_id,
            confirm_action_id=action_id,
        )
        a, b = await asyncio.gather(
            c.chat_service.executor.confirm(action_id, context, c.chat_service.spec),
            c.chat_service.executor.confirm(action_id, context, c.chat_service.spec),
        )
        assert a == b and "申请已提交" in a["answer"]
        replay = await c.chat_service.chat(confirmation, trace_id="t")
        assert replay.answer == a["answer"]
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ReturnRow)) == 1
            row = await db.get(ActionRow, action_id)
            assert row.status == "succeeded"
        facts = await c.chat_service.store.facts(context)
        assert facts["last_order"]["order_id"] == "ORD10002"
        ref = await c.chat_service.store.put({"private": "data"}, context)
        with pytest.raises(InvalidRequestError):
            await c.chat_service.store.read(ref, context.model_copy(update={"user_id": "other"}))
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_expired_and_changed_business_state(tmp_path):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/expired.db"),
        seed=True,
    )
    try:
        response = await c.chat_service.chat(
            ChatRequest(message="取消订单 ORD10003", user_id="u_demo"), trace_id="t"
        )
        action = response.pending_action
        context = ToolContext(user_id="u_demo", session_id=response.session_id, trace_id="t")
        async with c.session_factory() as db:
            await db.execute(
                update(ActionRow)
                .where(ActionRow.action_id == action.action_id)
                .values(expires_epoch=int(time.time()) - 1)
            )
            await db.commit()
        with pytest.raises(InvalidRequestError):
            await c.chat_service.executor.confirm(action.action_id, context, c.chat_service.spec)
    finally:
        await c.aclose()
