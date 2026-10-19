import json

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.core.exceptions import LLMProviderError
from app.providers.deepseek import DeepSeekProvider


@pytest.mark.asyncio
async def test_deepseek_forwards_tools_and_returns_tool_calls():
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

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    async with httpx.AsyncClient(
        base_url="https://provider.test", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekProvider(
            Settings(
                deepseek_api_key=SecretStr("test-only"),
                deepseek_base_url="https://provider.test",
                deepseek_model="deepseek-flash",
            ),
            client,
        )
        response = await provider.generate(
            [{"role": "user", "content": "查询退货政策"}], tools=tools, max_tokens=100
        )

    assert request_payload["model"] == "deepseek-flash"
    assert request_payload["tools"] == tools
    assert response.tool_calls == [tool_call]
    assert response.finish_reason == "tool_calls"
    assert response.usage.total_tokens == 22


@pytest.mark.asyncio
async def test_deepseek_rejects_truncated_completion():
    def handler(_request):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekProvider(Settings(deepseek_api_key=SecretStr("test-only")), client)
        with pytest.raises(LLMProviderError) as exc:
            await provider.generate([{"role": "user", "content": "hello"}])

    assert exc.value.finish_reason == "length"
