"""Qwen OpenAI-compatible chat-completion provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.config.settings import Settings
from app.core.exceptions import LLMProviderError
from app.core.retry import is_retryable_status, with_exponential_backoff
from app.providers.base import LLMResponse
from app.providers.deepseek import DeepSeekProvider
from app.schemas.common import TokenUsage


class QwenProvider(DeepSeekProvider):
    """Qwen's compatible endpoint, with its provider-specific thinking switch."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        # Reuse the OpenAI-compatible client's URL, timeout, key check and lifecycle.
        compatible = settings.model_copy(
            update={
                "deepseek_api_key": settings.qwen1_api_key,
                "deepseek_base_url": settings.qwen1_base_url,
                "deepseek_model": settings.qwen1_model,
                "deepseek_thinking": None,
            }
        )
        super().__init__(compatible, client)
        self._thinking_enabled = settings.qwen1_thinking == "enabled"

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, str] | None,
        tools: list[dict] | None = None,
        stream: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "enable_thinking": self._thinking_enabled,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
        return payload

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        api_key = self._settings.deepseek_api_key.get_secret_value()
        if not api_key:
            raise LLMProviderError("Qwen1 API key is not configured", retryable=False)
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            tools=tools,
        )

        async def _call() -> LLMResponse:
            try:
                response = await self._client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                raise LLMProviderError("Qwen1 request timed out", timeout=True) from exc
            except httpx.HTTPError as exc:
                raise LLMProviderError("Qwen1 transport error") from exc

            if response.status_code in {400, 401, 403, 404, 422}:
                raise LLMProviderError(f"Qwen1 HTTP {response.status_code}", retryable=False)
            if response.status_code == 429:
                raise LLMProviderError("Qwen1 rate limited", rate_limited=True)
            if is_retryable_status(response.status_code):
                raise LLMProviderError(f"Qwen1 HTTP {response.status_code}")
            if response.status_code >= 400:
                raise LLMProviderError(f"Qwen1 HTTP {response.status_code}", retryable=False)

            try:
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMProviderError("Qwen1 response missing choices", retryable=False) from exc
            finish_reason = choice.get("finish_reason", "stop")
            if finish_reason not in ("stop", "tool_calls"):
                raise LLMProviderError(
                    "Qwen1 response incomplete", retryable=False, finish_reason=finish_reason
                )
            usage_raw = body.get("usage") or {}
            usage = TokenUsage(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                total_tokens=int(usage_raw.get("total_tokens") or 0),
            )
            return LLMResponse(
                content=message.get("content") or "",
                usage=usage,
                model=self._settings.deepseek_model,
                tool_calls=message.get("tool_calls"),
                finish_reason=finish_reason,
            )

        return await with_exponential_backoff(
            _call,
            max_attempts=self._settings.llm_max_retries,
            operation_name="qwen1.generate",
        )

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        api_key = self._settings.deepseek_api_key.get_secret_value()
        if not api_key:
            raise LLMProviderError("Qwen1 API key is not configured", retryable=False)
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=True,
        )
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            ) as response:
                if response.status_code in {400, 401, 403, 404, 422}:
                    raise LLMProviderError(f"Qwen1 HTTP {response.status_code}", retryable=False)
                if response.status_code == 429:
                    raise LLMProviderError("Qwen1 rate limited", rate_limited=True)
                if is_retryable_status(response.status_code):
                    raise LLMProviderError(f"Qwen1 HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise LLMProviderError(f"Qwen1 HTTP {response.status_code}", retryable=False)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {}).get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield delta
        except httpx.TimeoutException as exc:
            raise LLMProviderError("Qwen1 stream timed out", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError("Qwen1 stream transport error") from exc
