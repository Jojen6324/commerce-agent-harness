import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.budget import BudgetedProvider
from app.providers.base import LLMResponse
from app.repositories.models import RunRow
from app.schemas.chat import ChatRequest
from app.schemas.common import TokenUsage
from app.services.container import build_container
from app.tools.base import ToolContext


def native(name, arguments, identifier):
    return LLMResponse(
        "",
        usage=TokenUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        tool_calls=[
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_b_requires_policy_agent_for_retrieval_and_keeps_orders_optional(tmp_path):
    settings = Settings(
        app_env="testing",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/b.db",
        agent_delegation_policy="required_when_eligible",
    )
    container = await build_container(settings, seed=True)
    assert "delegate_task" in container.chat_service.main_spec.tools
    assert container.chat_service.main_spec.delegate_agents == ("orders",)

    roles = Counter()
    prompts = {}
    dispatches = []
    original = container.chat_service.executor.execute

    async def audited(name, args, context, spec, **kwargs):
        dispatches.append((spec.name, name))
        return await original(name, args, context, spec, **kwargs)

    policy_ref = None

    async def generate(messages, **kwargs):
        nonlocal policy_ref
        system = messages[0]["content"]
        role = (
            "policy"
            if "你是只读政策研究专家" in system
            else "orders"
            if "你是只读订单专家" in system
            else "main"
        )
        roles[role] += 1
        prompts.setdefault(role, messages)

        if role == "policy":
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "answer_question" not in names
            assert names <= {"search_knowledge", "read_artifact"}
            if roles[role] == 1:
                return native(
                    "search_knowledge",
                    {"query": "耳机 七天无理由 签收次日起算"},
                    "policy-search",
                )
            tool_results = [
                json.loads(message["content"])
                for message in messages
                if message.get("role") == "tool"
            ]
            chunks = [
                chunk
                for result in tool_results
                for chunk in result.get("output", {}).get("chunks", [])
            ]
            assert chunks
            policy_ref = chunks[0]["chunk_id"]
            return LLMResponse(
                json.dumps(
                    {
                        "status": "sufficient",
                        "findings": [
                            {
                                "claim": "七天无理由期限从实际签收次日起算。",
                                "evidence_refs": [policy_ref],
                                "applicability": ["耳机"],
                                "exceptions": [],
                            }
                        ],
                        "missing_information": [],
                        "evidence_refs": [policy_ref],
                    }
                ),
                usage=TokenUsage(total_tokens=100),
            )

        if role == "main":
            delegate = next(
                tool["function"]
                for tool in kwargs["tools"]
                if tool["function"]["name"] == "delegate_task"
            )
            assert delegate["parameters"]["properties"]["agent"]["enum"] == ["orders"]
            main_context = "\n".join(message["content"] for message in messages)
            assert policy_ref in main_context
            return LLMResponse(
                f"七天无理由期限从实际签收次日起算。[{policy_ref}]",
                usage=TokenUsage(total_tokens=100),
            )

        raise AssertionError("Orders is available but is not required for this policy-only task")

    container.chat_service.executor.execute = audited
    container.chat_service.runner.llm = BudgetedProvider(
        SimpleNamespace(generate=AsyncMock(side_effect=generate))
    )
    try:
        response = await container.chat_service.chat(
            ChatRequest(message="耳机七天无理由从哪天起算？", user_id="u_demo"),
            trace_id="t",
        )
        delegation = response.diagnostics["delegation"]
        assert response.status == "completed"
        assert delegation["required_experts"] == ["policy"]
        assert delegation["required_experts_complete"] is True
        assert delegation["budget_blocked"] == []
        assert roles == Counter(policy=2, main=1)
        assert dispatches == [
            ("main", "delegate_task"),
            ("policy", "search_knowledge"),
        ]

        async with container.session_factory() as db:
            root_run = await db.get(RunRow, response.run_id)
            assert root_run.trace_id == response.trace_id and root_run.parent_run_id is None
            for attempt in delegation["attempts"]:
                child_run = await db.get(RunRow, attempt["child_run_id"])
                assert child_run.parent_run_id == response.run_id
                assert child_run.trace_id == response.trace_id

        stored_facts = await container.chat_service.store.facts(
            ToolContext(
                user_id="u_demo",
                session_id=response.session_id,
                trace_id=response.trace_id,
            )
        )
        policy_refs = [
            item
            for item in stored_facts.get("evidence_refs", [])
            if item.get("source_tool") == "search_knowledge"
        ]
        assert policy_refs and policy_refs[-1]["source_trace_id"] == response.trace_id
        assert "verified_observations" in "\n".join(m["content"] for m in prompts["policy"])
        assert policy_ref in "\n".join(m["content"] for m in prompts["main"])
    finally:
        await container.aclose()
