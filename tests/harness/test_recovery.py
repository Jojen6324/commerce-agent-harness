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


@pytest.mark.asyncio
async def test_changed_order_state_denies_confirmation(tmp_path):
    from app.repositories.models import OrderRow

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/changed.db"),
        seed=True,
    )
    try:
        r = await c.chat_service.chat(
            ChatRequest(message="取消订单 ORD10003", user_id="u_demo"), trace_id="t"
        )
        context = ToolContext(user_id="u_demo", session_id=r.session_id, trace_id="t")
        async with c.session_factory() as db:
            await db.execute(
                update(OrderRow).where(OrderRow.order_id == "ORD10003").values(status="shipped")
            )
            await db.commit()
        result = await c.chat_service.executor.confirm(
            r.pending_action.action_id, context, c.chat_service.spec
        )
        assert result["status"] == "denied"
        async with c.session_factory() as db:
            assert (await db.get(OrderRow, "ORD10003")).status == "shipped"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_process_exit_after_write_before_response_replays_once(tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "crash.db"
    settings = Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{path}")
    c = await build_container(settings, seed=True)
    r = await c.chat_service.chat(
        ChatRequest(message="订单 ORD10002", user_id="u_demo"), trace_id="t"
    )
    context = ToolContext(user_id="u_demo", session_id=r.session_id, trace_id="t")
    proposal = await c.chat_service.executor.execute(
        "create_return_request",
        {"order_id": "ORD10002", "reason": "质量问题"},
        context,
        c.chat_service.spec,
    )
    action = proposal["pending_action"]["action_id"]
    await c.aclose()
    script = """
import asyncio,os,sys
from app.config.settings import Settings
from app.services.container import build_container
from app.tools.base import ToolContext
async def go():
 c=await build_container(Settings(app_env='testing',database_url='sqlite+aiosqlite:///'+sys.argv[1]))
 await c.chat_service.executor.confirm(sys.argv[2],ToolContext(user_id='u_demo',session_id=sys.argv[3],trace_id='crash'),c.chat_service.spec)
 os._exit(0)
asyncio.run(go())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(path), action, r.session_id],
        capture_output=True,
        timeout=30,
        # The child deliberately exits without flushing coverage; measure the
        # observable committed database state in the parent instead.
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE_", "COVERAGE_"))},
            "AGNES": "",
        },
    )
    assert result.returncode == 0
    c = await build_container(settings)
    try:
        replay = await c.chat_service.executor.confirm(action, context, c.chat_service.spec)
        assert "申请已提交" in replay["answer"]
        async with c.session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(ReturnRow)) == 1
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_proposal_idempotency_key_preserves_parameters(tmp_path):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/idem.db"),
        seed=True,
    )
    try:
        context = ToolContext(
            user_id="u_demo", session_id="s", trace_id="t", idempotency_key="request1"
        )
        args = {"order_id": "ORD10002", "reason": "裂纹"}
        first = await c.chat_service.executor.execute(
            "create_return_request", args, context, c.chat_service.spec
        )
        second = await c.chat_service.executor.execute(
            "create_return_request", args, context, c.chat_service.spec
        )
        assert first == second
        with pytest.raises(InvalidRequestError, match="idempotency"):
            await c.chat_service.executor.execute(
                "create_return_request",
                {**args, "reason": "改了原因"},
                context,
                c.chat_service.spec,
            )
    finally:
        await c.aclose()
