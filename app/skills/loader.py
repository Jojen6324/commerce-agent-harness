"""Skill loading and matching."""

from __future__ import annotations

import json
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "definitions"


def load_skills() -> list[dict]:
    skills: list[dict] = []
    if not SKILLS_DIR.exists():
        return skills
    for path in sorted(SKILLS_DIR.glob("*.json")):
        skills.append(json.loads(path.read_text(encoding="utf-8")))
    return skills


def skill_by_name(name: str) -> dict | None:
    for skill in load_skills():
        if skill.get("name") == name:
            return skill
    return None


def match_skill(query: str) -> dict | None:
    """Optional trigger lookup; runtime loads detailed skills explicitly by name."""
    for skill in load_skills():
        if any(trigger in query for trigger in skill.get("triggers", [])):
            return skill
    return None
