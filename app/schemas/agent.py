"""Agent planning, intent, complexity and tool-result schemas."""

from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import IntentLabel, ToolRisk


class IntentResult(BaseModel):
    label: IntentLabel
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    prompt_version: str


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    name: str
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    risk: ToolRisk = ToolRisk.READ
    truncated: bool = False
