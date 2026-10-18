from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ToolPermissionError
from app.harness.delegation import DelegationService
from app.harness.executor import DelegateInput, ToolExecutor
from app.harness.runner import AgentSpec
from app.tools.aftersale_tools import CancelOrderTool
from app.tools.base import ToolContext
from app.tools.order_tools import GetOrderTool
from app.tools.registry import ToolRegistry


def context():
    return ToolContext(user_id="u", session_id="s", trace_id="t")


@pytest.mark.asyncio
async def test_specialists_cannot_write_or_delegate():
    registry = ToolRegistry()
    registry.register(GetOrderTool())
    registry.register(CancelOrderTool())
    executor = ToolExecutor(registry, None)
    delegate = DelegationService(None, executor, None)
    for spec in delegate.specs.values():
        assert "cancel_order" not in spec.tools and "delegate_task" not in spec.tools
        with pytest.raises(ToolPermissionError):
            await executor.execute("cancel_order", {"order_id": "ORD1"}, context(), spec)
    with pytest.raises(ToolPermissionError):
        await executor.execute(
            "delegate_task",
            {"agent": "orders", "goal": "查询"},
            context(),
            AgentSpec("orders", frozenset(["delegate_task"])),
        )


@pytest.mark.asyncio
async def test_specialist_failure_returns_bounded_structured_result():
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value={
                "status": "failed",
                "answer": "无法查询",
                "observations": [],
                "events": [],
                "run_id": "child",
            }
        )
    )
    executor = SimpleNamespace(registry=ToolRegistry())
    store = SimpleNamespace(record=AsyncMock())
    delegate = DelegationService(runner, executor, store)
    result = await delegate(DelegateInput(agent="orders", goal="查物流"), context())
    assert result["status"] == "failed" and result["missing_fields"]
    assert "messages" not in result
    store.record.assert_awaited_once()
