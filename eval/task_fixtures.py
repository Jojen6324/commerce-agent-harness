"""Deterministic, isolated business snapshots for task evaluation."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from app.repositories.models import (
    ActionRow,
    AuditLogRow,
    Base,
    OrderItemRow,
    OrderRow,
    ProductRow,
    RefundRow,
    ReturnRow,
)
from eval.task_schema import DATA_DIR

BUSINESS_MODELS = {
    m.__tablename__: m
    for m in (OrderRow, OrderItemRow, ProductRow, RefundRow, ReturnRow, AuditLogRow)
}
FIXTURES = json.loads((DATA_DIR / "agent_fixtures.json").read_text())


def resolve_profiles(names):
    result = []
    visiting = set()

    def visit(name):
        if name in result:
            return
        if name in visiting or name not in FIXTURES["profiles"]:
            raise ValueError(f"unknown/cyclic fixture {name}")
        visiting.add(name)
        for dependency in FIXTURES["profiles"][name]["depends_on"]:
            visit(dependency)
        visiting.remove(name)
        result.append(name)

    for name in names:
        visit(name)
    return result


def verify_knowledge():
    root = Path(__file__).resolve().parents[1]
    for path, digest in FIXTURES["profiles"]["knowledge_v1"]["files"].items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"knowledge fixture drift: {path}")


async def install_fixture(factory, names):
    profiles = resolve_profiles(names)
    if "knowledge_v1" in profiles:
        verify_knowledge()
    async with factory() as db:
        # All data are in an evaluator-owned temporary SQLite database. Never call
        # this against application/user databases or mutate a retained live fixture.
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(delete(table))
        for name in profiles:
            for table, rows in FIXTURES["profiles"][name].items():
                if table not in BUSINESS_MODELS:
                    continue
                model = BUSINESS_MODELS[table]
                for record in rows:
                    values = {
                        k: (datetime.fromisoformat(v) if k.endswith("_at") and v else v)
                        for k, v in record.items()
                    }
                    db.add(model(**values))
                await db.flush()
        await db.commit()


async def snapshot(factory):
    tables = {}
    async with factory() as db:
        for name, model in {**BUSINESS_MODELS, "harness_actions": ActionRow}.items():
            rows = []
            for row in (await db.scalars(select(model))).all():
                rows.append(
                    {
                        col.name: (
                            getattr(row, col.name).isoformat()
                            if isinstance(getattr(row, col.name), datetime)
                            else getattr(row, col.name)
                        )
                        for col in model.__table__.columns
                    }
                )
            tables[name] = rows
    return tables


def matches(row, values):
    return all(row.get(k) == v for k, v in values.items())


def business_deltas(before, after):
    changes = []
    for name, model in BUSINESS_MODELS.items():
        primary = [c.name for c in model.__table__.primary_key.columns]

        def key(row, columns=tuple(primary)):
            return tuple(row[k] for k in columns)

        left = {key(r): r for r in before[name]}
        right = {key(r): r for r in after[name]}
        for k in left.keys() - right.keys():
            changes.append({"table": name, "kind": "delete", "before": left[k], "after": {}})
        for k in right.keys() - left.keys():
            changes.append({"table": name, "kind": "insert", "before": {}, "after": right[k]})
        for k in right.keys() & left.keys():
            if right[k] != left[k]:
                changes.append(
                    {"table": name, "kind": "update", "before": left[k], "after": right[k]}
                )
    return changes


def validate_changes(before, after, rules):
    deltas = business_deltas(before, after)
    counts = [0] * len(rules)
    failures = []
    for delta in deltas:
        matched = False
        for i, rule in enumerate(rules):
            if (
                rule.table != delta["table"]
                or rule.kind != delta["kind"]
                or not matches(delta["after"], rule.where)
            ):
                continue
            if not matches(delta["after"], rule.fields):
                continue
            if delta["kind"] == "update":
                fields = {k for k, v in delta["after"].items() if delta["before"].get(k) != v}
                if not fields.issubset(rule.fields):
                    continue
            counts[i] += 1
            matched = True
            break
        if not matched:
            failures.append(f'unexpected {delta["kind"]} in {delta["table"]}')
    for rule, count in zip(rules, counts, strict=True):
        if count != rule.count:
            failures.append(f"{rule.table} {rule.kind}: expected {rule.count}, observed {count}")
    return failures, deltas
