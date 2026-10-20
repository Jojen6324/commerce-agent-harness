"""Retrieval-only knowledge tool plus a non-registered compatibility adapter."""

from pydantic import BaseModel, Field

from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext


class KnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    complex: bool = Field(default=False, description="多条件、政策例外或跨文档核对时启用多轮检索")


class EvidenceTool(Tool):
    name = "search_knowledge"
    description = (
        "只读政策证据检索：对本次 query 执行 BM25/BGE 混合召回、RRF 融合和 Cross-Encoder 重排，"
        "返回完整原文及 document_id、chunk_id、标题、版本、品类、条件和例外。"
        "一次调用只检索一次，不改写 query、不判断充分性、不生成答案；若缺少条件，请由 Agent 自行构造新 query 再调用。"
        "空结果仅表示未匹配，不能据此认定政策不存在；政策证据不代表订单事实。"
    )
    input_model = KnowledgeInput

    def __init__(self, service):
        self.service = service

    async def execute(self, parsed: KnowledgeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            name=self.name,
            success=True,
            output=await self.service.retrieve_evidence(parsed.query, complex=parsed.complex),
        )


class PolicyAnswerTool(EvidenceTool):
    """Compatibility tool for explicit callers; never registered in ChatService."""

    name = "answer_question"
    description = "纯政策问答的完整RAG：检索、精排、多轮补检索、生成及忠实度自纠错。返回已校验答案和原始证据；只读。"

    async def execute(self, parsed: KnowledgeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            name=self.name,
            success=True,
            output=await self.service.answer_question(
                parsed.query,
                complex=parsed.complex,
                user_question=context.user_messages[-1] if context.user_messages else parsed.query,
            ),
        )
