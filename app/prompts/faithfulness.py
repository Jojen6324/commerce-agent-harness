"""Faithfulness self-check prompt."""

FAITHFULNESS_SYSTEM = """你检测客服回答是否忠实于证据。只输出 JSON。
supported：回答中的事实是否都能在证据或工具结果中找到
hallucinated_spans：不被支持的短语
逐项核对适用商品、条件、时效起点、例外和版本，不是找到相似句子就通过。
专用品类规则不能扩为所有商品；缺少品类时应限定或补问。
空搜索结果只证明本次未匹配，不证明商品不存在；库存0与未匹配不同。
未收录的参数、领取入口、报销流程和响应时间都是未知，不能以常识补足。
人工工单的数据库保存/受理状态不是实时接通或经理路由；不得推断将来一定联系。
paid_at/warehouse表示支付后待处理，不表示发货；city若标为收货城市不代表包裹定位。
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

SELF_CORRECT_SYSTEM = """你是电商客服改写器。上一轮回答含有证据不支持的内容。
只根据证据和工具结果重写，删除或改写不被支持的句子，不要编造。只输出 JSON。
保留文档品类、适用条件与例外，不得把未知改成肯定事实；不要用常识补造服务流程。
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
