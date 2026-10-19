import pytest

from app.core.enums import ToolRisk
from app.core.exceptions import InvalidRequestError
from app.schemas.chat import ActionProposal, ChatRequest


def _action() -> ActionProposal:
    return ActionProposal(
        action_id="act_test",
        tool_name="cancel_order",
        arguments={"order_id": "ORD10003"},
        risk=ToolRisk.DESTRUCTIVE,
        summary="确认后将取消订单 ORD10003。",
    )


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_session_and_user(tmp_path):
    from app.config.settings import Settings
    from app.services.container import build_container

    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/actions.db"),
        seed=True,
    )
    try:
        response = await c.chat_service.chat(
            ChatRequest(message="取消订单 ORD10003", user_id="u_demo"), trace_id="t"
        )
        assert response.pending_action
        confirmed = ChatRequest(
            message="确认执行",
            user_id="u_demo",
            session_id=response.session_id,
            confirm_action_id=response.pending_action.action_id,
        )
        with pytest.raises(InvalidRequestError):
            await c.chat_service.chat(
                confirmed.model_copy(update={"user_id": "someone_else"}), trace_id="t"
            )
        with pytest.raises(InvalidRequestError):
            await c.chat_service.chat(
                confirmed.model_copy(update={"session_id": "other"}), trace_id="t"
            )
        result = await c.chat_service.chat(confirmed, trace_id="t")
        assert "已取消" in result.answer
    finally:
        await c.aclose()
