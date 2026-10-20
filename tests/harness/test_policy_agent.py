import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.harness.delegation import DelegationService
from app.harness.executor import DelegateInput
from app.harness.runner import AgentRunner
from app.providers.base import LLMResponse
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry


def _call(name, arguments, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _chunk(chunk_id, content):
    return {
        "chunk_id": chunk_id,
        "document_id": "returns-policy-v3",
        "title": "退换货政策",
        "content": content,
        "policy_version": "v3",
        "category": "return_policy",
        "section": "耳机",
        "metadata": {"applicable_category": "耳机"},
    }


class _PolicyExecutor:
    def __init__(self):
        self.registry = ToolRegistry()
        self.artifacts = None
        self.queries = []

    def schemas(self, spec):
        assert spec.name == "policy"
        assert spec.tools == {"search_knowledge", "read_artifact"}
        return []

    async def execute(self, name, args, context, spec):
        assert spec.name == "policy"
        assert name == "search_knowledge"
        self.queries.append(args["query"])
        chunk_id = f"policy-{len(self.queries)}"
        return {
            "success": True,
            "output": {"chunks": [_chunk(chunk_id, f"原文支持查询：{args['query']}")]},
        }


def _context():
    return ToolContext(user_id="u_demo", session_id="s1", trace_id="trace1")


@pytest.mark.asyncio
async def test_policy_agent_follows_up_with_new_query_and_returns_verified_structured_findings():
    final = {
        "status": "sufficient",
        "findings": [
            {
                "claim": "耳机未拆封时可以申请无理由退货。",
                "evidence_refs": ["policy-1"],
                "applicability": ["耳机", "未拆封"],
                "exceptions": ["已激活设备"],
            },
            {
                "claim": "已激活设备属于例外。",
                "evidence_refs": ["policy-2"],
                "applicability": ["耳机"],
                "exceptions": [],
            },
        ],
        "missing_information": [],
        "evidence_refs": ["policy-1", "policy-2"],
    }
    provider = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    tool_calls=[_call("search_knowledge", {"query": "耳机无理由退货 条件"}, "s1")],
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    "",
                    tool_calls=[_call("search_knowledge", {"query": "耳机 激活 退货例外"}, "s2")],
                    finish_reason="tool_calls",
                ),
                LLMResponse(json.dumps(final, ensure_ascii=False)),
            ]
        )
    )
    executor = _PolicyExecutor()
    runner = AgentRunner(provider, executor, Settings())
    store = SimpleNamespace(record=AsyncMock())
    service = DelegationService(runner, executor, store)

    result = await service(
        DelegateInput(agent="policy", goal="核实耳机退货条件及激活例外"),
        _context(),
    )

    assert executor.queries == ["耳机无理由退货 条件", "耳机 激活 退货例外"]
    assert result["status"] == "completed"
    assert result["policy_result_status"] == "sufficient"
    assert result["evidence_refs"] == ["policy-1", "policy-2"]
    assert [finding["claim"] for finding in result["findings"]] == [
        "耳机未拆封时可以申请无理由退货。",
        "已激活设备属于例外。",
    ]
    assert [chunk["chunk_id"] for chunk in result["chunks"]] == ["policy-1", "policy-2"]
    assert (
        "PolicyResearchResult" in service.specs["policy"].prompt
        or "政策研究" in service.specs["policy"].prompt
    )


@pytest.mark.asyncio
async def test_policy_agent_deduplicates_repeated_queries_and_rejects_fabricated_refs():
    final = {
        "status": "sufficient",
        "findings": [
            {
                "claim": "可以申请退货。",
                "evidence_refs": ["policy-1", "fabricated"],
                "applicability": [],
                "exceptions": [],
            }
        ],
        "missing_information": [],
        "evidence_refs": ["policy-1", "fabricated"],
    }
    provider = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                LLMResponse(
                    "",
                    tool_calls=[_call("search_knowledge", {"query": "退货条件"}, "first")],
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    "",
                    tool_calls=[_call("search_knowledge", {"query": " 退货条件 "}, "repeat")],
                    finish_reason="tool_calls",
                ),
                LLMResponse(json.dumps(final, ensure_ascii=False)),
            ]
        )
    )
    executor = _PolicyExecutor()
    runner = AgentRunner(provider, executor, Settings())
    service = DelegationService(runner, executor, None)

    result = await service(
        DelegateInput(agent="policy", goal="核实退货条件"),
        _context(),
    )

    assert executor.queries == ["退货条件"]
    assert result["status"] == "incomplete"
    assert result["evidence_refs"] == ["policy-1"]
    assert result["findings"] == []
    assert "invalid_or_missing_evidence_reference" in result["missing_fields"]
    assert provider.generate.await_count == 3


@pytest.mark.asyncio
async def test_policy_agent_stops_after_three_distinct_searches():
    refs = ["policy-1", "policy-2", "policy-3"]
    final = {
        "status": "sufficient",
        "findings": [
            {
                "claim": "已核实相关政策条款。",
                "evidence_refs": refs,
                "applicability": [],
                "exceptions": [],
            }
        ],
        "missing_information": [],
        "evidence_refs": refs,
    }
    responses = [
        LLMResponse(
            "",
            tool_calls=[_call("search_knowledge", {"query": f"退货条件 {index}"}, f"q{index}")],
            finish_reason="tool_calls",
        )
        for index in range(1, 5)
    ]
    responses.append(LLMResponse(json.dumps(final, ensure_ascii=False)))
    provider = SimpleNamespace(generate=AsyncMock(side_effect=responses))
    executor = _PolicyExecutor()
    result = await DelegationService(AgentRunner(provider, executor, Settings()), executor, None)(
        DelegateInput(agent="policy", goal="核实退货政策"), _context()
    )

    assert executor.queries == ["退货条件 1", "退货条件 2", "退货条件 3"]
    assert result["status"] == "completed"
    assert result["evidence_refs"] == refs
