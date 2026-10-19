"""Keep document scope and provenance attached throughout RAG."""

import re

from app.schemas.retrieval import RetrievedChunk


def cited_refs(answer: str) -> set[str]:
    refs = set()
    for bracket in re.findall(r"\[([^\]\n]+)\]", answer):
        for ref in re.split(r"[,，;；\s]+", bracket):
            if re.fullmatch(r"[A-Za-z0-9_-]+", ref):
                refs.add(ref)
    return refs


def evidence_text(chunk: RetrievedChunk) -> str:
    title = chunk.metadata.get("document_title", chunk.title)
    source = chunk.metadata.get("source", chunk.document_id)
    return (
        f"[{chunk.chunk_id}] 文档：{title}；章节：{chunk.section or '未标注'}；"
        f"来源：{source}；版本：{chunk.policy_version}\n"
        "适用范围须由文档标题、章节和正文共同判断，专用品类规则不可泛化。\n"
        f"{chunk.content}"
    )
