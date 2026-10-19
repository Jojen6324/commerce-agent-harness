"""Human-readable annotations and response-bound supplemental semantic review."""

import argparse
import hashlib
import json
from pathlib import Path

from eval.task_schema import DATA_DIR, load_tasks


def corpus_digest():
    digest = hashlib.sha256()
    for name in ("agent_tasks.jsonl", "agent_task_splits.json", "agent_fixtures.json"):
        digest.update((DATA_DIR / name).read_bytes())
    return digest.hexdigest()


def response_digest(case):
    values = [
        {"request": t.get("request"), "response": t.get("response"), "status": t.get("http_status")}
        for t in case["turns"]
    ]
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def render_catalog(tasks):
    lines = [
        "# Agent任务逐条审阅目录",
        "",
        "数据由开发助手逐问编写，供人工审核；不是已完成独立人工全量验收的标签。原始问题不变，未按intent映射工具。",
        "所有派生记录保留同一source_id；更大的split_group同时锁定近似原问。语义审阅不覆盖确定性失败。",
        "",
    ]
    for task in tasks:
        lines += [
            f"## {task.id} · {task.source_id} · {task.variant}",
            f"- 场景：{task.scenario}；集合：{task.split}；分组：{task.split_group}",
            f'- 来源/派生：{", ".join(task.derived_from) or "原问"}；{task.derivation}',
            f'- Fixture：{", ".join(task.fixtures)}',
            f"- 任务结果：{task.expected_outcome}",
            "- 关键事实：",
        ]
        lines += [f"  - {f.statement}（{f.source}）" for f in task.critical_facts]
        lines += ["- 禁止的副作用："] + [f"  - {f}" for f in task.forbidden_effects]
        if task.forbidden_claims:
            lines += [
                "- 禁止结论审阅信号（须判断否定词，不仅查子串）："
                + "；".join(task.forbidden_claims)
            ]
        for i, turn in enumerate(task.turns, 1):
            lines += [
                f"- 第{i}轮 [{turn.user_id}]：{turn.message}",
                f"  - 期望：{turn.expected_outcome}；HTTP {turn.http_status}",
            ]
            if turn.confirm_from_turn:
                lines += [
                    f"  - 仅确认第{turn.confirm_from_turn}轮正确参数的action_id；错误提案不得执行。"
                ]
            lines += [
                f"  - 验收 {c.kind} {json.dumps(c.value,ensure_ascii=False)}：{c.note}"
                for c in turn.checks
            ]
            for path in turn.alternatives:
                lines += [f"  - 可接受结果路径：{path.description}"]
                lines += [
                    f"    - {c.kind} {json.dumps(c.value,ensure_ascii=False)}：{c.note}"
                    for c in path.checks
                ]
            lines += [
                f"  - 允许变更：{json.dumps([r.model_dump() for r in turn.allowed_changes],ensure_ascii=False)}（其他业务变更均禁止）"
            ]
        lines += [
            "- 补充语义审阅：" + (task.semantic_review or "无强制语义项；仍保留异常结论审阅信号。"),
            "",
        ]
    return "\n".join(lines) + "\n"


def review_template(report):
    return {
        "corpus_digest": report["corpus_digest"],
        "reviews": [
            {
                "task_id": case["id"],
                "response_digest": response_digest(case),
                "reviewer": "",
                "decision": None,
                "rationale": "",
                "criterion": case["review_criterion"],
            }
            for case in report["cases"]
            if case["semantic_status"] == "pending"
        ],
    }


def apply_reviews(report, labels):
    import copy

    from eval.behavior import summarize

    if (
        labels["corpus_digest"] != report["corpus_digest"]
        or report["corpus_digest"] != corpus_digest()
    ):
        raise ValueError("review/corpus mismatch")
    result = copy.deepcopy(report)
    cases = {case["id"]: case for case in result["cases"]}
    seen = set()
    for review in labels["reviews"]:
        task_id = review["task_id"]
        if task_id in seen or task_id not in cases:
            raise ValueError("unknown or duplicate review task")
        seen.add(task_id)
        case = cases[task_id]
        if review["response_digest"] != response_digest(case):
            raise ValueError("review belongs to a different response/run")
        if review["decision"] is None:
            continue
        if (
            review["decision"] not in ("pass", "fail")
            or not review["reviewer"].strip()
            or not review["rationale"].strip()
        ):
            raise ValueError("review requires pass/fail, reviewer and rationale")
        case["semantic_status"] = "passed" if review["decision"] == "pass" else "failed"
        case["semantic_review"] = {
            k: review[k] for k in ("reviewer", "rationale", "decision", "response_digest")
        }
        if case["deterministic_status"] == "passed":
            case["task_status"] = "passed" if review["decision"] == "pass" else "failed"
        # Manual semantics can never override an observed invariant violation.
    result.update(summarize(result["cases"], load_tasks()))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.catalog:
        args.output.write_text(render_catalog(load_tasks()))
        return
    data = json.loads(args.report.read_text())
    report = data.get("behavior", data)
    result = (
        apply_reviews(report, json.loads(args.reviews.read_text()))
        if args.reviews
        else review_template(report)
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
