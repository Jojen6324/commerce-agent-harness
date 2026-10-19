"""Faithfulness self-check prompt."""

from app.prompts.evidence_rules import EVIDENCE_RULES

FAITHFULNESS_SYSTEM = f"""你检测客服回答是否忠实于证据。只输出 JSON。
supported：回答中的事实是否都能在证据或工具结果中找到
hallucinated_spans：不被支持的短语
逐项核对以下规则。发现矛盾或遗漏会改变用户办理结果的必要条件时，不能仅因其他部分有来源就判supported=true。
{EVIDENCE_RULES}
保持JSON精简：rationale不超过80字，hallucinated_spans至多3个简短原文片段。禁止输出长篇分析；格式精简不降低事实校验标准。
"""

FAITHFULNESS_USER = """问题：{query}
回答：{answer}

证据：
{evidence}

工具结果：
{tool_results}

输出 JSON：
{{"supported":true,"hallucinated_spans":[],"rationale":"..."}}
"""

SELF_CORRECT_SYSTEM = f"""你是电商客服改写器。上一轮回答含有证据不支持的内容。
只根据证据和工具结果重写，删除或改写不被支持的句子，不要编造。保留由证据明确前提和时效起点推导出的确定结论，不要把可直接计算的结论弱化成“可能”或“若确实”。只输出 JSON。
保留文档品类、适用条件与例外，不得把未知改成肯定事实；不要用常识补造服务流程。
原答复含必要补问时保留补问，不能把需要用户提供信息的事项改写为已完成。
{EVIDENCE_RULES}
"""

SELF_CORRECT_USER = """问题：{query}
不被支持的原回答：{answer}
裁判说明：{critique}

证据：
{evidence}

工具结果：
{tool_results}

输出 JSON：
{{"answer":"...","citation_chunk_ids":[]}}
"""
