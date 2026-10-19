import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.config.settings import Settings
from app.harness.context import ContextBuilder
from app.main import create_app
from app.providers.base import LLMResponse
from app.providers.fake import OfflineLLMProvider
from app.repositories.db import create_engine, create_session_factory
from app.repositories.models import AuditLogRow, OrderRow, SupportTicketRow
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import RetrievedChunk
from app.services.container import build_container


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["inline", "verified_payload", "uncited"])
async def test_chat_only_reports_sources_used_by_the_final_answer(tmp_path, mode):
    c = await build_container(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/citations.db",
        )
    )
    try:
        chunks = [
            RetrievedChunk(
                document_id="policy",
                chunk_id=ref,
                title="政策",
                content="需提交凭证",
                score=1,
            ).model_dump(mode="json")
            for ref in ("used", "unused")
        ]
        answer = "需提交凭证" + (" [used]" if mode == "inline" else "")
        data = {"chunks": chunks}
        if mode == "verified_payload":
            data.update(
                answer=answer,
                validated=True,
                answered_question="如何办理",
                citations=[
                    {"chunk_id": "used"},
                ],
            )
        c.chat_service.runner.run = AsyncMock(
            return_value={
                "status": "completed",
                "run_id": "run_sources",
                "events": [],
                "answer": answer,
                "observations": [
                    {
                        "name": "search_knowledge",
                        "output": {"success": True, "output": data},
                    }
                ],
            }
        )
        c.chat_service.validator.llm = OfflineLLMProvider()
        response = await c.chat_service.chat(
            ChatRequest(message="如何办理", user_id="u"), trace_id="t"
        )
        assert [citation.chunk_id for citation in response.citations] == (
            [] if mode == "uncited" else ["used"]
        )
    finally:
        await c.aclose()


def native(name, args):
    return LLMResponse(
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_durable_support_inbox_permissions_dedup_claim_and_resolve(tmp_path):
    settings = Settings(
        app_env="testing",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/support.db",
        admin_api_token=SecretStr("test-staff-token"),
    )
    c = await build_container(settings, seed=True)
    ticket_id = None
    try:
        model = SimpleNamespace(
            generate=AsyncMock(
                return_value=native("escalate_to_human", {"reason": "用户要求人工审核退款"})
            )
        )
        c.chat_service.runner.llm = model
        c.chat_service.validator.llm = SimpleNamespace(
            generate=AsyncMock(
                side_effect=AssertionError("database receipt must not require another model call")
            )
        )
        app = create_app(settings)
        app.state.container = c
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/v1/chat", json={"message": "请人工审核退款", "user_id": "u_demo"}
            )
            assert created.status_code == 200
            body = created.json()
            assert "待人工受理" in body["answer"] and "尚未接通" in body["answer"]
            assert body["pending_action"] is None
            async with c.session_factory() as db:
                tickets = list((await db.scalars(select(SupportTicketRow))).all())
                assert len(tickets) == 1
                ticket_id = tickets[0].ticket_id
                assert tickets[0].session_id == body["session_id"]
                assert tickets[0].user_id == "u_demo"
                assert (await db.get(OrderRow, "ORD10001")).status == "shipped"
            repeat = await client.post(
                "/api/v1/chat",
                json={
                    "message": "请人工审核退款",
                    "user_id": "u_demo",
                    "session_id": body["session_id"],
                },
            )
            assert ticket_id in repeat.json()["answer"]
            base = "/api/v1/admin/support-tickets"
            assert (await client.get(base)).status_code == 401
            assert (await client.get(base, headers={"x-admin-token": "wrong"})).status_code == 401
            headers = {"x-admin-token": "test-staff-token"}
            listed = (await client.get(base, headers=headers)).json()["tickets"]
            assert len(listed) == 1 and listed[0]["ticket_id"] == ticket_id
            detail = (await client.get(f"{base}/{ticket_id}", headers=headers)).json()
            assert detail["messages"] and detail["ticket"]["reason"] == "用户要求人工审核退款"
            claim = {
                "expected_status": "pending",
                "status": "in_progress",
                "operator_id": "staff-1",
            }
            assert (await client.patch(f"{base}/{ticket_id}", json=claim, headers=headers)).json()[
                "assigned_to"
            ] == "staff-1"
            assert (
                await client.patch(
                    f"{base}/{ticket_id}", json={**claim, "operator_id": "staff-2"}, headers=headers
                )
            ).status_code == 400
            resolve = {
                "expected_status": "in_progress",
                "status": "resolved",
                "operator_id": "staff-1",
                "resolution": "已人工核实，待用户补凭证",
            }
            assert (
                await client.patch(
                    f"{base}/{ticket_id}",
                    json={**resolve, "operator_id": "staff-2"},
                    headers=headers,
                )
            ).status_code == 400
            assert (
                await client.patch(
                    f"{base}/{ticket_id}", json={**resolve, "resolution": ""}, headers=headers
                )
            ).status_code == 400
            assert (
                await client.patch(f"{base}/{ticket_id}", json=resolve, headers=headers)
            ).json()["status"] == "resolved"
            assert not (await client.get(base, headers=headers)).json()["tickets"]
    finally:
        await c.aclose()
    # Reopen the database in a new engine, so a process-local dictionary cannot pass.
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            row = await db.get(SupportTicketRow, ticket_id)
            assert row.status == "resolved" and row.assigned_to == "staff-1"
            assert len((await db.scalars(select(SupportTicketRow))).all()) == 1
            assert (
                len(
                    (
                        await db.scalars(
                            select(AuditLogRow).where(
                                AuditLogRow.action == "support_ticket_transition"
                            )
                        )
                    ).all()
                )
                == 2
            )
    finally:
        await engine.dispose()


def test_inline_result_cannot_be_replaced_with_a_null_artifact_reference():
    content = json.dumps(
        {"output": {"answer": "签收次日起七天，超过时效不能无理由退货", "padding": "x" * 2600}},
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": "r"},
        {"role": "user", "content": "签收8天能退吗", "current_task": True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": content},
    ]
    prepared, _ = ContextBuilder(4000).build(messages, [])
    assert prepared[-1]["content"] == content
    wrapped = {
        **json.loads(content),
        "artifact_ref": "art_real",
        "summary": "已过七天，不能无理由退货",
        "evidence_refs": ["return_01"],
    }
    messages[-1]["content"] = json.dumps(wrapped, ensure_ascii=False)
    prepared, changed = ContextBuilder(4000).build(messages, [])
    assert changed
    result = json.loads(prepared[-1]["content"])
    assert result["artifact_ref"] == "art_real" and result["evidence_refs"] == ["return_01"]
    assert "已过七天" in result["summary"]


@pytest.mark.asyncio
async def test_catalog_policy_and_logistics_contracts(tmp_path):
    from app.core.request_scope import request_session
    from app.tools.base import ToolContext

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/facts.db"),
        seed=True,
    )
    try:
        context = ToolContext(user_id="u_demo", session_id="session", trace_id="test")
        async with request_session(c.session_factory):
            found = await c.chat_service.registry.get("search_products").run(
                {"query": "定制杯"}, context
            )
            assert any(p["sku"] == "SKU-MUG-CUSTOM" for p in found.output["products"])
            detail = await c.chat_service.registry.get("get_product_detail").run(
                {"sku": "SKU-HEADSET"}, context
            )
            assert detail.output["technical_specifications"] == "not_recorded"
            logistics = await c.chat_service.registry.get("track_shipment").run(
                {"order_id": "ORD10001"}, context
            )
            assert logistics.output["events"][0]["time_source"] == "paid_at"
            assert logistics.output["events"][1]["time_source"] == "shipped_at"
            assert "收货城市" in logistics.output["city_meaning"]
        coupons = await c.chat_service.registry.get("get_coupon").run({}, context)
        assert any("不适用于定制商品" in chunk["content"] for chunk in coupons.output["chunks"])
        assert coupons.output["limitations"]["claim_entry"] == "not_recorded"
        from app.retrieval.evidence import evidence_text
        from app.schemas.retrieval import RetrievedChunk

        evidence = evidence_text(RetrievedChunk.model_validate(coupons.output["chunks"][0]))
        assert "优惠券规则" in evidence and "来源：" in evidence and "适用范围" in evidence
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_ticket_read_is_bound_to_owner_and_conversation(tmp_path):
    from app.core.exceptions import InvalidRequestError
    from app.core.request_scope import request_session
    from app.repositories.support import SupportRepository
    from app.tools.base import ToolContext
    from app.tools.support import GetSupportTicketTool

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/scope.db")
    )
    try:
        async with request_session(c.session_factory) as db:
            ticket = await SupportRepository(db).submit(
                user_id="owner", session_id="original", reason="人工审核"
            )
            tool = GetSupportTicketTool()
            context = ToolContext(user_id="owner", session_id="original", trace_id="t")
            assert (await tool.run({"ticket_id": ticket["ticket_id"]}, context)).output[
                "status"
            ] == "pending"
            for update in [{"user_id": "other"}, {"session_id": "other"}]:
                with pytest.raises(InvalidRequestError, match="not found"):
                    await tool.run(
                        {"ticket_id": ticket["ticket_id"]}, context.model_copy(update=update)
                    )
    finally:
        await c.aclose()


def test_optional_handoff_offer_is_not_an_execution_claim():
    from app.harness.completion import completion_issue

    assert completion_issue("如果您需要，我可以为您创建工单。") is None
    assert completion_issue("正在为您接通人工客服，请稍等。") == "missing_handoff_receipt"
    assert completion_issue("收到，我可以为您创建转人工工单。") == "missing_handoff_receipt"
