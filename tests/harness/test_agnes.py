import json

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.core.exceptions import LLMProviderError
from app.providers.agnes import AgnesProvider


@pytest.mark.asyncio
async def test_provider_native_calls_usage_and_truncation():
    sent = []

    async def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_order",
                                        "arguments": '{"order_id":"ORD1"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://apihub.agnes-ai.com/v1"
    ) as client:
        provider = AgnesProvider(Settings(agnes=SecretStr("test-only")), client)
        response = await provider.generate(
            [{"role": "user", "content": "查询"}], tools=[{"type": "function"}]
        )
        assert response.tool_calls[0]["id"] == "c1" and response.usage.total_tokens == 19
        assert sent[0]["model"] == "agnes-3.0-flash"
        assert await provider.ping()

    async def truncated(request):
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(truncated), base_url="https://apihub.agnes-ai.com/v1"
    ) as client:
        with pytest.raises(LLMProviderError, match="incomplete"):
            await AgnesProvider(Settings(), client).generate([])


@pytest.mark.asyncio
async def test_nonretryable_auth_failure_does_not_retry():
    calls = 0

    async def reject(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reject), base_url="https://apihub.agnes-ai.com/v1"
    ) as client:
        with pytest.raises(LLMProviderError, match="401"):
            await AgnesProvider(Settings(), client).generate([])
    assert calls == 1
