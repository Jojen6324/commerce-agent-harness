"""Self-RAG retrieval grading prompt."""

GRADE_SYSTEM = """你评估检索结果是否足以回答问题。只输出 JSON。
relevant：是否至少有一条证据相关
sufficient：是否足以给出不编造的回答
missing_aspect：若不足，还缺什么
必须核对证据文档标题、品类和章节的适用范围；数码、食品、跨境、定制等专用规则不可当通用规则。
未确定用户商品类型时，证据须足以给出带条件的结论或指出应补问品类。
"""

GRADE_USER = """问题：{query}

证据：
{evidence}

输出 JSON：
{{"relevant":true,"sufficient":true,"missing_aspect":"","rationale":"..."}}
"""
