import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError, LLMProviderError
from app.core.request_scope import request_session
from app.guardrails.hallucination import FaithfulnessChecker
from app.harness.budget import BudgetedProvider, BudgetExceeded, RunBudget, current_budget
from app.providers.base import LLMResponse
from app.repositories.models import ProductRow
from app.services.container import build_container
from app.tools.base import ToolContext


@pytest.mark.asyncio
async def test_length_recovery_requires_a_complete_verdict_and_preserves_rejection():
    model = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMProviderError("incomplete", retryable=False, finish_reason="length"),
                LLMResponse(
                    '{"supported":false,"hallucinated_spans":["所有商品"],"rationale":"专用规则被泛化"}'
                ),
            ]
        )
    )
    verdict = await FaithfulnessChecker(model).check(
        query="能退吗", answer="所有商品可退", chunks=[], tool_results_text=""
    )
    assert verdict.supported is False and verdict.rationale == "专用规则被泛化"
    assert model.generate.await_count == 2
    assert model.generate.await_args_list[1].kwargs["max_tokens"] == 768
    # Repeated bad JSON cannot be silently accepted or retried without bound.
    model.generate = AsyncMock(return_value=LLMResponse('{"supported":'))
    verdict = await FaithfulnessChecker(model).check(
        query="q", answer="a", chunks=[], tool_results_text=""
    )
    assert not verdict.supported and verdict.failure["attempts"] == 2
    assert model.generate.await_count == 2


@pytest.mark.asyncio
async def test_checker_does_not_add_retries_for_auth_or_rate_limit_and_honors_parent_budget():
    for error in [
        LLMProviderError("HTTP401", retryable=False),
        LLMProviderError("HTTP429", rate_limited=True),
    ]:
        model = SimpleNamespace(generate=AsyncMock(side_effect=error))
        verdict = await FaithfulnessChecker(model).check(
            query="q", answer="a", chunks=[], tool_results_text=""
        )
        assert not verdict.supported and model.generate.await_count == 1
    model = SimpleNamespace(
        generate=AsyncMock(side_effect=LLMProviderError("length", finish_reason="length"))
    )
    budget = RunBudget(time.monotonic() + 10, max_models=1)
    token = current_budget.set(budget)
    try:
        with pytest.raises(BudgetExceeded):
            await FaithfulnessChecker(BudgetedProvider(model)).check(
                query="q", answer="a", chunks=[], tool_results_text=""
            )
        assert budget.models == 1 and model.generate.await_count == 1
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_recommendation_filters_before_limit_and_does_not_relax_explicit_constraints(
    tmp_path,
):
    c = await build_container(
        Settings(app_env="testing", database_url=f"sqlite+aiosqlite:///{tmp_path}/recommend.db"),
        seed=True,
    )
    try:
        async with c.session_factory() as db:
            for i in range(5):
                db.add(
                    ProductRow(
                        sku=f"EMPTY{i}", name="礼物", category="家居", price_cents=1, inventory=0
                    )
                )
            db.add(
                ProductRow(
                    sku="EDGE", name="百元礼物", category="家居", price_cents=10000, inventory=1
                )
            )
            db.add(
                ProductRow(
                    sku="OVER", name="超预算", category="家居", price_cents=10001, inventory=10
                )
            )
            await db.commit()
        context = ToolContext(user_id="u_demo", session_id="s", trace_id="t")
        tool = c.chat_service.registry.get("recommend_products")
        async with request_session(c.session_factory):
            found = await tool.run(
                {"query": "100元以内有货礼物", "max_price_cents": 10000, "in_stock_only": True},
                context,
            )
            products = found.output["products"]
            assert {p["sku"] for p in products} == {"SKU-MUG-CUSTOM", "EDGE"}
            assert all(p["price_cents"] <= 10000 and p["inventory"] > 0 for p in products)
            assert found.output["matching_mode"] == "constraints_only"
            assert found.output["applied_filters"]["max_price_cents"] == 10000
            none = await tool.run({"category": "数码", "max_price_cents": 10000}, context)
            assert none.output["products"] == []
            with pytest.raises(InvalidRequestError):
                await tool.run({"max_price_cents": -1}, context)
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_real_provider_finish_reason_reaches_bounded_checker_recovery():
    import httpx
    from pydantic import SecretStr

    from app.providers.agnes import AgnesProvider

    limits = []

    def handler(request):
        import json

        limits.append(json.loads(request.content)["max_tokens"])
        first = len(limits) == 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if first else "stop",
                        "message": {
                            "content": '{"supported":'
                            if first
                            else '{"supported":true,"rationale":"证据支持"}'
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test", transport=httpx.MockTransport(handler)
    ) as client:
        provider = AgnesProvider(Settings(agnes=SecretStr("test-only")), client)
        verdict = await FaithfulnessChecker(provider).check(
            query="q", answer="a", chunks=[], tool_results_text=""
        )
    assert verdict.supported and limits == [512, 768]
