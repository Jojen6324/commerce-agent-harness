"""AGNES OpenAI-compatible provider; one concurrent request, no provider failover."""

import asyncio
import json

import httpx

from app.core.exceptions import LLMProviderError
from app.harness.budget import current_budget
from app.memory.budget import estimate_tokens
from app.providers.base import LLMResponse
from app.providers.deepseek import DeepSeekProvider
from app.schemas.common import TokenUsage


class AgnesProvider(DeepSeekProvider):
    def __init__(self, settings, client=None):
        # Reuse the existing provider's settings/client lifecycle and stream contract.
        compatible = settings.model_copy(
            update={
                "deepseek_api_key": settings.agnes,
                "deepseek_base_url": settings.agnes_base_url,
                "deepseek_model": settings.agnes_model,
            }
        )
        super().__init__(compatible, client)
        self._gate = asyncio.Semaphore(1)

    async def generate(
        self, messages, *, temperature=0.0, max_tokens=1024, response_format=None, tools=None
    ):
        payload = dict(
            model=self._settings.deepseek_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
        async with self._gate:
            attempts = min(3, max(1, self._settings.llm_max_retries))
            for attempt in range(attempts):
                if attempt and current_budget.get() is not None:
                    current_budget.get().reserve_model(
                        estimate_tokens(json.dumps(payload, ensure_ascii=False)) + max_tokens
                    )
                try:
                    response = await self._client.post(
                        "/chat/completions",
                        json=payload,
                        headers={
                            "Authorization": "Bearer "
                            + self._settings.deepseek_api_key.get_secret_value()
                        },
                    )
                except httpx.HTTPError as exc:
                    if attempt == attempts - 1:
                        raise LLMProviderError("AGNES transport failed") from exc
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                if response.status_code >= 400:
                    retry = response.status_code == 429 or response.status_code >= 500
                    if retry and attempt < attempts - 1:
                        delay = 2.0 * 2**attempt
                        try:
                            delay = max(
                                delay, min(30.0, float(response.headers.get("Retry-After", 0)))
                            )
                        except ValueError:
                            pass
                        await asyncio.sleep(delay)
                        continue
                    raise LLMProviderError(
                        f"AGNES HTTP {response.status_code}",
                        rate_limited=response.status_code == 429,
                        retryable=retry,
                    )
                try:
                    body = response.json()
                    choice = body["choices"][0]
                    message = choice["message"]
                    finish = choice.get("finish_reason", "stop")
                    # Truncated output cannot enter either JSON or tool execution paths.
                    if finish not in ("stop", "tool_calls"):
                        raise LLMProviderError(
                            "AGNES incomplete response", retryable=False, finish_reason=finish
                        )
                    raw = body.get("usage") or {}
                    usage = TokenUsage(
                        **{
                            k: int(raw.get(k) or 0)
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        }
                    )
                    return LLMResponse(
                        message.get("content") or "",
                        usage,
                        self._settings.deepseek_model,
                        message.get("tool_calls"),
                        finish,
                    )
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMProviderError("AGNES malformed response", retryable=False) from exc
        raise LLMProviderError("AGNES unavailable")
