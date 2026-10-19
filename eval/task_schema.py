"""Reviewable outcome-oriented task corpus, without runtime intent labels."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA_DIR = Path(__file__).parent / "dataset"
HISTORICAL_SHA256 = "b342f37a65972bba787262a01df0fe7e28ceb930a50ae6a826189f3ecc39dbff"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(StrictModel):
    statement: str = Field(min_length=1)
    source: str = Field(min_length=1)


class Check(StrictModel):
    kind: Literal[
        "answer_any",
        "answer_not",
        "target_observed",
        "evidence",
        "proposal",
        "no_proposal",
        "refused",
        "status",
        "db_count",
        "handoff_ticket",
    ]
    value: Any = None
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assertion(self):
        if self.kind in ("answer_any", "answer_not", "evidence", "status"):
            if (
                not isinstance(self.value, list)
                or not self.value
                or not all(isinstance(v, str) for v in self.value)
            ):
                raise ValueError("check requires a nonempty string list")
        if self.kind == "target_observed" and (not isinstance(self.value, dict) or not self.value):
            raise ValueError("target check requires explicit parameter values")
        if self.kind == "proposal" and (
            not isinstance(self.value, dict)
            or not isinstance(self.value.get("tool_name"), str)
            or not isinstance(self.value.get("arguments"), dict)
        ):
            raise ValueError("proposal requires operation and parameters")
        if self.kind == "refused" and not isinstance(self.value, bool):
            raise ValueError("refused requires bool")
        if self.kind == "db_count" and (
            not isinstance(self.value, dict)
            or self.value.get("table")
            not in (
                "orders",
                "order_items",
                "products",
                "refunds",
                "returns",
                "audit_logs",
                "harness_actions",
            )
            or not isinstance(self.value.get("where"), dict)
            or not isinstance(self.value.get("count"), int)
            or self.value["count"] < 0
        ):
            raise ValueError("db_count requires table, where and nonnegative count")
        return self


class ChangeRule(StrictModel):
    table: Literal["orders", "order_items", "products", "refunds", "returns", "audit_logs"]
    kind: Literal["update", "insert"]
    where: dict[str, Any]
    fields: dict[str, Any] = Field(default_factory=dict)
    count: int = Field(default=1, ge=1)


class AcceptancePath(StrictModel):
    description: str = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)


class Turn(StrictModel):
    message: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    user_id: str = "u_demo"
    http_status: list[int] = Field(default_factory=lambda: [200])
    confirm_from_turn: int | None = Field(default=None, ge=1)
    checks: list[Check]
    allowed_changes: list[ChangeRule] = Field(default_factory=list)
    alternatives: list[AcceptancePath] = Field(default_factory=list)


class AgentTask(StrictModel):
    id: str
    source_id: str
    variant: Literal["original", "derived"]
    derived_from: list[str]
    derivation: str
    scenario: str
    split_group: str
    split: Literal["development", "holdout"]
    fixtures: list[str] = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    critical_facts: list[Fact] = Field(min_length=1)
    forbidden_effects: list[str] = Field(min_length=1)
    forbidden_claims: list[str] = Field(default_factory=list)
    semantic_review: str
    turns: list[Turn] = Field(min_length=1)


def load_tasks(path: Path | None = None) -> list[AgentTask]:
    tasks = [
        AgentTask.model_validate_json(line)
        for line in (path or DATA_DIR / "agent_tasks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    history_path = DATA_DIR / "intent_100.jsonl"
    if hashlib.sha256(history_path.read_bytes()).hexdigest() != HISTORICAL_SHA256:
        raise ValueError("historical baseline changed")
    source = {
        r["id"]: r
        for line in history_path.read_text().splitlines()
        if line.strip()
        for r in [json.loads(line)]
    }
    manifest = json.loads((DATA_DIR / "agent_task_splits.json").read_text())
    family = {
        sid: (group["id"], group["split"])
        for group in manifest["groups"]
        for sid in group["source_ids"]
    }
    if len(family) != len(source) or set(family) != set(source):
        raise ValueError("split manifest must cover each historical source exactly once")
    if sum(len(g["source_ids"]) for g in manifest["groups"]) != len(source):
        raise ValueError("duplicate source in split groups")
    ids = {t.id for t in tasks}
    if len(ids) != len(tasks):
        raise ValueError("duplicate task id")
    fixture_definitions = json.loads((DATA_DIR / "agent_fixtures.json").read_text())["profiles"]
    originals = {}
    for t in tasks:
        if any(name not in fixture_definitions for name in t.fixtures):
            raise ValueError(f"{t.id}: unknown fixture")
        if t.source_id not in source or (t.split_group, t.split) != family[t.source_id]:
            raise ValueError(f"{t.id}: family leakage or invalid provenance")
        if t.variant == "original":
            if (
                t.derived_from
                or t.turns[0].message != source[t.source_id]["query"]
                or t.source_id in originals
            ):
                raise ValueError(f"{t.id}: original question changed or duplicated")
            originals[t.source_id] = t.id
        else:
            if not t.derived_from or not t.derivation:
                raise ValueError(f"{t.id}: missing derivation")
            for parent in t.derived_from:
                match = next((p for p in tasks if p.id == parent), None)
                if (
                    match is None
                    or match.variant != "original"
                    or match.source_id != t.source_id
                    or match.split != t.split
                ):
                    raise ValueError(f"{t.id}: invalid/cross-family derivation")
        for index, turn in enumerate(t.turns, 1):
            if turn.confirm_from_turn is not None and turn.confirm_from_turn >= index:
                raise ValueError(f"{t.id}: confirmation must reference an earlier turn")
    if set(originals) != set(source):
        raise ValueError("must preserve all 100 original questions")
    return tasks
