"""Full evidence and validated policy answer tools (read-only)."""

from pydantic import BaseModel, Field

from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext


class KnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    complex: bool = Field(default=False, description="多条件、政策例外或跨文档核对时启用多轮检索")


class EvidenceTool(Tool):
    name = "search_knowledge"
    description = "完整政策检索：改写、BM25/BGE、RRF、精排、按需多轮自纠错。返回原文、来源、版本和chunk_id；不代表订单事实。只读。"
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
    name = "answer_question"
    description = "纯政策问答的完整RAG：检索、精排、多轮补检索、生成及忠实度自纠错。返回已校验答案和原始证据；只读。"

    async def execute(self, parsed: KnowledgeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            name=self.name,
            success=True,
            output=await self.service.answer_question(parsed.query, complex=parsed.complex),
        )
