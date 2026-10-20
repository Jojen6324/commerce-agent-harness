import json

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.providers.qwen import QwenProvider
from app.services.container import build_llm_provider


@pytest.mark.asyncio
async def test_qwen_provider_disables_thinking_and_parses_tool_calls():
    request_payload = {}
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_knowledge", "arguments": '{"query":"退货政策"}'},
    }

    def handler(request):
        request_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": None, "tool_calls": [tool_call]},
                    }
                ],
                "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = QwenProvider(
            Settings(
                qwen1_api_key=SecretStr("test-only"),
                qwen1_base_url="https://provider.test/v1",
                qwen1_model="qwen3.8-flash",
                qwen1_thinking="disabled",
            ),
            client,
        )
        response = await provider.generate(
            [{"role": "user", "content": "查询退货政策"}],
            tools=[{"type": "function", "function": {"name": "search_knowledge"}}],
        )

    assert request_payload["model"] == "qwen3.8-flash"
    assert request_payload["enable_thinking"] is False
    assert "thinking" not in request_payload
    assert response.tool_calls == [tool_call]
    assert response.finish_reason == "tool_calls"
    assert response.usage.total_tokens == 22


@pytest.mark.asyncio
async def test_container_selects_qwen_when_explicitly_configured():
    settings = Settings(
        app_env="development",
        agnes=SecretStr(""),
        deepseek_api_key=SecretStr(""),
        qwen1_api_key=SecretStr("test-only"),
    )
    provider = build_llm_provider(settings)
    assert isinstance(provider, QwenProvider)
    await provider.aclose()
