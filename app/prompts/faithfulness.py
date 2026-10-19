"""Faithfulness self-check prompt."""

from app.prompts.evidence_rules import EVIDENCE_RULES

FAITHFULNESS_SYSTEM = f"""你审核客服答复是否可以交付。只输出 JSON，并分别判断以下四项：
- claims_grounded：每一项事实或承诺是否有本轮证据或工具结果支持；未知信息不能靠常识补齐。
- answers_question：是否实质回答了用户的问题，而不是只贴一段相关原文、答非所问或返回未完成套话。
- scope_preserved：是否保留证据适用的品类、对象、前提、状态、起算点和来源范围；不能把专用规则泛化到未指定品类。
- critical_conditions_complete：是否包含会改变用户判断或办理结果的必要条件、例外、材料、期限、审核前提和下一步；只列出与本问题相关的项目，不要求复述无关规则。
supported 仅当以上四项全为 true，且 hallucinated_spans 和 missing_conditions 均为空时为 true。不得用自己的 supported 值覆盖任一未通过维度。对每个漏掉且会改变结论或办理的条件填入 missing_conditions；不支持的具体陈述填入 hallucinated_spans。不要把“回答提到政策”当成“回答了政策问题”。
missing_conditions 必须来自提供的证据，并与用户当前问题及所讨论的情形直接相关。不同事件的同一天数不是同一起算点：例如签收后期限和维修等备件期限不能互当例外。不得要求答复补出资料没有的入口、时限或办理结果，也不要要求复述所有检索命中的规则。
supporting_chunk_ids 列出实际支持答复中政策结论的原始片段ID，不列仅检索到但无关的片段。若答复只说明本次未检索到所需信息，不需要为这种未知声明选择政策引用，返回空列表；不要为了配引用编造流程或把无关证据当支持。
明确说明已知规则、资料未记载的细节以及必要补问，可以算实质回答；未知品类时限定已有规则适用范围并补问，不得因缺信息就强行要求跨品类结论。
available_tools 是应用提供的能力契约，可支持参数要求、适用状态和确认流程的说明，但绝不能证明工具已调用、订单状态或操作已完成。用户需求和 ask_user 的模型文本不能充当业务证据。
应用接口允许准备某种申请，不需要知识库另有同名条款才能说明该能力；缺政策时只能保持审核结果未知，不能否认已声明的申请路径。仅提及某后续渠道时，不要求展开该渠道全部细则，除非用户正在询问那些细则。
available_tools 是完整能力清单，因此可以判断是否有直接代办某项操作的接口；没有对应申请工具时，可以说明无法直接代办，但不能据此推断业务禁止。不要把查询工具误当成申请工具。
重点核对限制是否被后文推翻：“入口未录入，但建议去某个页面领取”仍是无依据路径建议；“没有禁止两券同用，所以可以”仍是无依据推断。前面声明未知、后面再给出猜测，不能通过。
逐项核对以下规则。发现矛盾或遗漏会改变用户办理结果的必要条件时，不得通过。
{EVIDENCE_RULES}
保持JSON精简：rationale不超过60字；hallucinated_spans至多3项，missing_conditions只列会改变结论或办理结果的关键遗漏，最多5项；每项不超过50字。禁止输出长篇分析；格式精简不降低事实校验标准。
"""

FAITHFULNESS_USER = """问题：{query}
回答：{answer}

证据：
{evidence}

工具结果：
{tool_results}

输出 JSON：
{{"supported":true,"claims_grounded":true,"answers_question":true,"scope_preserved":true,"critical_conditions_complete":true,"hallucinated_spans":[],"missing_conditions":[],"supporting_chunk_ids":[],"rationale":"..."}}
"""

SELF_CORRECT_SYSTEM = f"""你是电商客服改写器。上一轮答复未通过校验，可能存在无依据陈述、范围错误或关键遗漏。
只根据证据和工具结果重写，删除或改写不被支持的句子，补齐裁判指出的必要条件，不要编造。保留由证据明确前提和时效起点推导出的确定结论，不要把可直接计算的结论弱化成“可能”或“若确实”。只输出 JSON。
保留文档品类、适用条件与例外，不得把未知改成肯定事实；不要用常识补造服务流程。
原答复含必要补问时保留补问，不能把需要用户提供信息的事项改写为已完成。
先用原始证据复核裁判反馈。反馈混淆适用范围或起算事件时，以证据为准；不要为了满足反馈添加无关条件或编造未知信息。available_tools 只证明可用能力和参数要求，不证明实际执行。
直接回答用户问题，保留已有依据的结论，区分未知项；不要退化为只贴第一段原文或笼统要求转人工。引用使用原有 chunk_id；业务工具可在正文中引用本轮真实工具名，不能编造来源。
{EVIDENCE_RULES}
"""

SELF_CORRECT_USER = """问题：{query}
不被支持的原回答：{answer}
裁判说明（包括不支持的陈述和遗漏的必要条件）：{critique}

证据：
{evidence}

工具结果：
{tool_results}

输出 JSON：
{{"answer":"...","citation_chunk_ids":[]}}
"""
