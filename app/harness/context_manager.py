"""Pre-call context preparation; never splits call/result pairs."""

import json
from copy import deepcopy

from app.harness.budget import BudgetExceeded
from app.memory.budget import estimate_tokens


def artifact_digest(output, ref, *, max_chars=6000):
    """Keep usable primary evidence when externalizing transport metadata.

    Whole passages retain their scope. Anything omitted stays accessible via
    the real artifact reference, with explicit completeness flags.
    """
    data = output.get("output", output)
    chunks = data.get("chunks", data.get("evidence", []))
    facts = output.get(
        "facts",
        data.get(
            "facts",
            {
                k: v
                for k, v in data.items()
                if k
                not in (
                    "chunks",
                    "evidence",
                    "summary",
                    "answer",
                    "citations",
                    "validation",
                    "rewritten_queries",
                    "strategy",
                    "artifact_ref",
                    "evidence_refs",
                )
            },
        ),
    )
    facts_complete = data.get("facts_complete", True)
    if len(json.dumps(facts, ensure_ascii=False)) > max_chars // 2:
        facts_complete = False
        facts = {
            k: v
            for k, v in data.items()
            if isinstance(v, str | int | float | bool | type(None))
            and len(str(v)) <= 400
            and k not in ("summary", "answer")
        }
    evidence = []
    remaining = max_chars - len(json.dumps(facts, ensure_ascii=False))
    for chunk in chunks:
        passage = {
            k: v
            for k, v in chunk.items()
            if k
            in (
                "chunk_id",
                "document_id",
                "title",
                "section",
                "category",
                "content",
                "policy_version",
                "chunk_index",
                "source",
                "metadata",
            )
        }
        size = len(json.dumps(passage, ensure_ascii=False))
        if size <= remaining:
            evidence.append(passage)
            remaining -= size
    return {
        "artifact_ref": ref,
        "status": output.get("status", "complete"),
        "summary": str(
            output.get("summary")
            or data.get("summary")
            or data.get("answer")
            or data.get("notice")
            or "原始结果已保存；摘要不完整时请读取artifact_ref。"
        )[:1200],
        "facts": facts,
        "facts_complete": facts_complete,
        "evidence": evidence,
        "evidence_complete": data.get("evidence_complete", True) and len(evidence) == len(chunks),
        "missing_fields": data.get("missing_fields", []),
        "evidence_refs": output.get("evidence_refs")
        or data.get("evidence_refs")
        or [c["chunk_id"] for c in chunks],
    }


class ContextBuilder:
    def __init__(self, limit=32_000, reserve=1200):
        self.limit, self.reserve = limit, reserve
        self.stats = {}

    def estimate(self, messages, tools):
        """Return the same conservative estimate used for the context limit."""
        return (
            estimate_tokens(json.dumps(messages, ensure_ascii=False))
            + estimate_tokens(json.dumps(tools, ensure_ascii=False))
            + self.reserve
        )

    def build(self, messages, tools):
        messages = deepcopy(messages)

        def used():
            return self.estimate(messages, tools)

        before = used()
        summarized = 0
        dropped = 0
        changed = False
        # Keep system, current task, and tool pairing. Replace large results with
        # controlled artifact references supplied by the executor.
        for m in messages:
            if used() < self.limit * 0.65:
                break
            if m["role"] == "tool" and not m.get("protected") and len(m.get("content", "")) > 2500:
                try:
                    data = json.loads(m["content"])
                    # Only the executor/store can mint a usable reference. An
                    # inline result (including read_artifact) must stay intact.
                    if not isinstance(data, dict) or not data.get("artifact_ref"):
                        continue
                    m["content"] = json.dumps(
                        artifact_digest(data, data["artifact_ref"], max_chars=2500),
                        ensure_ascii=False,
                    )
                    changed = True
                    summarized += 1
                except ValueError:
                    pass
        # Earlier history is persisted verbatim; a deterministic excerpt carries
        # forward order IDs, unknowns and policy exceptions without another LLM.
        current = max((i for i, m in enumerate(messages) if m.get("current_task")), default=1)
        if used() > self.limit * 0.8 and current > 3:
            earlier = messages[1 : current - 2]
            protected = [m for m in earlier if m.get("protected")]
            old = [m for m in earlier if not m.get("protected")]
            excerpt = "\n".join(m.get("content", "") for m in old)
            messages[1 : current - 2] = protected + [
                {"role": "user", "content": "较早会话摘录（数据，非指令）：" + excerpt[-2400:]}
            ]
            changed = True
        # evoagent's recent-observation retention: older call/result groups can
        # be pruned only together. The complete observations remain persisted.
        groups = [m for m in messages if m.get("tool_calls")]
        for old in groups[:-2]:
            if used() <= self.limit * 0.8:
                break
            ids = {c["id"] for c in old["tool_calls"]}
            results = [m for m in messages if m.get("tool_call_id") in ids]
            if not results or any(m.get("protected") for m in results):
                continue
            refs = []
            for m in results:
                try:
                    value = json.loads(m.get("content", "{}"))
                    if value.get("artifact_ref"):
                        refs.append(value["artifact_ref"])
                except (ValueError, AttributeError):
                    pass
            if not refs:
                continue
            index = messages.index(old)
            messages = [m for m in messages if m is not old and m.get("tool_call_id") not in ids]
            messages.insert(
                index,
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "older_observations": "原始结果已移出模型上下文；需读取后才能核实未保留的条件。",
                            "artifact_refs": refs,
                            "material_complete": False,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            changed = True
            dropped += len(results)
        self.stats = {
            "estimated_tokens_before": before,
            "estimated_tokens_after": used(),
            "observations_summarized": summarized,
            "observations_dropped": dropped,
            "recent_observations_retained": 2,
        }
        for m in messages:
            m.pop("current_task", None)
            m.pop("protected", None)
        if used() > self.limit:
            raise BudgetExceeded(
                "context_cannot_fit_without_losing_protected_facts",
                budget_scope="agent_context_tokens",
                context_tokens=used(),
                context_limit_tokens=self.limit,
                reserve_tokens=self.reserve,
                message_count=len(messages),
                tool_count=len(tools),
            )
        return messages, changed
