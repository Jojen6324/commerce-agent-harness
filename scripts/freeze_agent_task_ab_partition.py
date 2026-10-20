"""Freeze the pre-evaluation A/B and B-only task membership.

This script reads, but never edits, the two source corpora. It writes a
content-hashed JSON manifest and a human-reviewable Markdown catalog.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from eval.task_schema import AgentTask

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ROOT / "eval/dataset/agent_tasks.jsonl",
    ROOT / "eval/dataset/agent_comparison_50.jsonl",
)
OUTPUT_JSON = ROOT / "eval/dataset/agent_task_ab_partition.json"
OUTPUT_MD = ROOT / "eval/dataset/agent_task_ab_partition_review.md"
EXPECTED_SOURCE_SHA256 = {
    "eval/dataset/agent_tasks.jsonl": "64be6784da387ce264c518b6b3e11c79f959188fa5da21e58eb4d1a23ab56282",
    "eval/dataset/agent_comparison_50.jsonl": "376f0d6889862624f848832a409b687c511a503fb2a7207bf44e41f1346630bb",
}
FROZEN_DATE = "2026-10-20"

MANUAL_POLICY_ADD: dict[str, str] = {
    "agent_013": "客服时段回答依赖知识库；知识文档与本地服务接口的时段口径还不一致，需先核对证据再说明差异。",
    "agent_031": "退款到账时间取决于审核状态和支付渠道，任务要求解释这些条件，必须依据退款时效政策。",
    "agent_036": "服饰能否退取决于签收天数、吊牌、洗涤和二次销售状态；任务允许条件式政策解释，需核对服饰退换细则。",
    "agent_044": "用户询问未发货订单的关闭方式，回答需说明取消资格与确认流程；这是店铺业务规则问题，不是订单事实查询。",
    "agent_045": "用户询问刚付款能否取消，回答需适用订单状态对应的取消条件；缺少具体订单时也需要给出有依据的条件说明。",
    "agent_056": "用户询问目前有哪些优惠券，答复依赖优惠券种类、门槛和限制等知识库规则。",
    "agent_060": "新客券领取涉及每账号领取次数、满减条件和适用范围，需检索优惠券规则；页面入口未记录时应明确未知。",
    "agent_062": "任务结果允许解释支付后48小时催发规则；投诉是否符合时限需对照配送政策，而非只凭用户评价。",
    "agent_088": "退货地址不能从现有业务记录直接编造；答复需核对售后审核/取件政策并说明知识库没有真实仓库地址。",
    "agent_097": "涉嫌欺诈属于升级投诉，任务事实明确指向投诉分级与经理接入时限，需要依据投诉SLA并避免断言欺诈成立。",
    "agent_032_refund": "补齐订单与裂纹原因后，是否适用定制商品质量例外及退款/退货条件会影响待确认方案，需核对售后政策。",
    "agent_095_missing_reason": "定制杯的裂纹属于质量问题主张；生成退款提案前需区分定制商品无理由退货限制与质量问题例外。",
    "dialogue_006": "电子发票所需公司名称、税号、地址电话及开户行信息由开票规则定义；第二轮还需指出缺少税号。",
    "dialogue_012": "ORD10002 是定制杯且用户要求仅退款不退货；裂纹质量问题能否走该路径需按定制售后和退款规则判断。",
}

# These are the manually reviewed, non-policy cases. IDs only key the frozen
# decisions; the reason is tied to each task's actual request, evidence and
# acceptance path, never to its scenario label.
NO_POLICY_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "conversation_only": (
        "本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。",
        tuple([f"agent_{i:03}" for i in range(1, 11)] + ["agent_100"]),
    ),
    "verified_order_facts": (
        "所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。",
        (
            "agent_021",
            "agent_024",
            "agent_029",
            "agent_021_tracking",
            "agent_033_requested",
            "collab_014",
            "dialogue_001",
        ),
    ),
    "catalog_and_inventory_facts": (
        "所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。",
        (
            "agent_046",
            "agent_047",
            "agent_048",
            "agent_049",
            "agent_050",
            "agent_052",
            "agent_053",
            "agent_055",
            "agent_091",
            "agent_092",
            "agent_092_alternative",
            "dialogue_007",
        ),
    ),
    "clarify_before_policy_decision": (
        "按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。",
        (
            "agent_032",
            "agent_033",
            "agent_037",
            "agent_041",
            "agent_042",
            "agent_086",
            "agent_086_tracking_id",
            "agent_095",
            "agent_096",
            "exception_004",
        ),
    ),
    "action_state_and_confirmation": (
        "任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。",
        (
            "agent_043",
            "agent_024_cancel",
            "agent_043_repeat_confirm",
            "agent_043_foreign_confirm",
            "dialogue_011",
            "dialogue_013",
        ),
    ),
    "complaint_acknowledgement_or_clarification": (
        "本题要求承认体验、澄清订单/事实或记录投诉意愿；验收不要求承诺具体SLA、补偿资格或政策结论。",
        ("agent_061", "agent_063", "agent_064", "agent_065"),
    ),
    "handoff_capability": (
        "请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。",
        (
            "agent_066",
            "agent_067",
            "agent_068",
            "agent_069",
            "agent_070",
            "agent_098",
            "agent_070_review_not_write",
        ),
    ),
    "safety_privacy_or_out_of_scope": (
        "任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。",
        (
            "agent_071",
            "agent_072",
            "agent_073",
            "agent_074",
            "agent_075",
            "agent_076",
            "agent_077",
            "agent_076_injection_order",
            "agent_099",
            "exception_001",
            "exception_002",
            "exception_003",
        ),
    ),
}


def read_sources() -> tuple[list[dict], dict[str, dict]]:
    tasks: list[dict] = []
    source_meta: dict[str, dict] = {}
    for path in SOURCES:
        raw = path.read_bytes()
        parsed = [
            AgentTask.model_validate_json(line).model_dump(mode="json")
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        ]
        rel = path.relative_to(ROOT).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != EXPECTED_SOURCE_SHA256[rel]:
            raise ValueError(f"source changed since partition review: {rel}")
        source_meta[rel] = {
            "sha256": digest,
            "total": len(parsed),
            "development": sum(row["split"] == "development" for row in parsed),
            "holdout": sum(row["split"] == "holdout" for row in parsed),
            "scenario_counts": dict(sorted(Counter(row["scenario"] for row in parsed).items())),
        }
        for row in parsed:
            row["source_file"] = rel
            tasks.append(row)
    ids = [row["id"] for row in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate IDs across source datasets")
    return tasks, source_meta


def evidence_checks(task: dict) -> list[dict]:
    return [
        {"turn": index, "values": check["value"]}
        for index, turn in enumerate(task["turns"], 1)
        for check in turn["checks"]
        if check["kind"] == "evidence"
    ]


def decide(task: dict) -> dict:
    if task["split"] == "holdout":
        return {
            "group": "holdout_excluded",
            "basis": "holdout_excluded",
            "reason": "保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。",
            "evidence_checks": evidence_checks(task),
        }
    checks = evidence_checks(task)
    if checks:
        values = sorted({value for item in checks for value in item["values"]})
        return {
            "group": "ab_comparison",
            "basis": "explicit_evidence_check",
            "reason": "源任务验收明确要求政策证据引用（evidence check: "
            + ", ".join(values)
            + "）；此任务同时运行A与B。",
            "evidence_checks": checks,
        }
    if task["id"] in MANUAL_POLICY_ADD:
        return {
            "group": "ab_comparison",
            "basis": "manual_policy_reasoning_review",
            "reason": MANUAL_POLICY_ADD[task["id"]],
            "evidence_checks": [],
        }
    for code, (reason, ids) in NO_POLICY_GROUPS.items():
        if task["id"] in ids:
            return {
                "group": "b_only",
                "basis": "manual_no_policy_evidence_review",
                "review_code": code,
                "reason": reason,
                "evidence_checks": [],
            }
    raise ValueError(f"no manual decision for development task {task['id']}")


def build() -> tuple[dict, str]:
    tasks, source_meta = read_sources()
    entries = []
    for task in tasks:
        decision = decide(task)
        entries.append(
            {
                "id": task["id"],
                "source_file": task["source_file"],
                "source_id": task["source_id"],
                "variant": task["variant"],
                "scenario": task["scenario"],
                "split_group": task["split_group"],
                "split": task["split"],
                "fixtures": task["fixtures"],
                "user_messages": [turn["message"] for turn in task["turns"]],
                "group": decision["group"],
                "basis": decision["basis"],
                "review_code": decision.get("review_code"),
                "reason": decision["reason"],
                "evidence_checks": decision["evidence_checks"],
            }
        )
    counts = Counter(entry["group"] for entry in entries)
    development = [entry for entry in entries if entry["split"] == "development"]
    explicit = [entry for entry in development if entry["basis"] == "explicit_evidence_check"]
    manual_add = [
        entry for entry in development if entry["basis"] == "manual_policy_reasoning_review"
    ]
    if (len(entries), len(development), counts) != (
        166,
        135,
        Counter({"ab_comparison": 66, "b_only": 69, "holdout_excluded": 31}),
    ):
        raise ValueError(
            f"unexpected partition counts: all={len(entries)}, dev={len(development)}, groups={counts}"
        )
    if len(explicit) != 52 or len(manual_add) != 14:
        raise ValueError(
            f"unexpected candidate basis counts: explicit={len(explicit)}, manual={len(manual_add)}"
        )

    def grouped_counts(field: str) -> dict[str, dict[str, int]]:
        grouped: dict[str, Counter] = defaultdict(Counter)
        for row in development:
            grouped[row[field]][row["group"]] += 1
        return {key: dict(sorted(value.items())) for key, value in sorted(grouped.items())}

    manifest = {
        "schema_version": 1,
        "frozen_at": FROZEN_DATE,
        "selection_status": "frozen_before_real_evaluation",
        "evaluation_started": False,
        "holdout_executed": False,
        "sources": source_meta,
        "selection_rule": {
            "explicit_evidence_definition": "A task is initially selected for A/B if any turn contains checks[].kind == 'evidence'. This produces 23 tasks from agent_tasks.jsonl and 29 from agent_comparison_50.jsonl, exactly 52.",
            "manual_review": "Every remaining development task was reviewed using its user messages, expected outcome, critical facts, checks, fixtures, and applicable knowledge documents. Add only when a successful answer or safe proposal depends on applying a store policy/evidence rule; scenario labels and task IDs are not selection criteria.",
            "ab_comparison": "Run both A (single Main Agent, delegation OFF) and B (mandatory Policy Agent for retrieval needs; Orders Agent remains optional). Faithfulness configuration is identical in A and B.",
            "b_only": "Run B only. These tasks do not require policy retrieval for their accepted outcome; Orders Agent remains optional.",
            "holdout": "Keep the original 21 + 10 Holdout tasks listed for provenance only; do not run them or use them to adjust this partition.",
        },
        "quality_metrics": {
            "policy": "For each applicable checks[].kind == evidence assertion, count a hit when at least one expected document family is cited by a chunk that resolves to the fixture index. Record check-level coverage and citation integrity. Manual policy-reasoning additions without explicit evidence checks remain separately marked not_scored and are excluded from the evidence-coverage denominator.",
            "faithfulness": "Record the production Faithfulness Agent/Checker verdict from response events or validation diagnostics, including issues and evidence references. Do not add a second judge call. Keep pass, fail, insufficient_evidence, unavailable, and not_run distinct; unavailable and not_run are not passes and are excluded from the judged-turn pass-rate denominator.",
        },
        "summary": {
            "total_records": len(entries),
            "development_tasks": len(development),
            "explicit_evidence_candidates": len(explicit),
            "manual_policy_reasoning_additions": len(manual_add),
            "ab_comparison": counts["ab_comparison"],
            "b_only": counts["b_only"],
            "holdout_excluded": counts["holdout_excluded"],
            "by_source_file": grouped_counts("source_file"),
            "development_by_scenario_and_group": grouped_counts("scenario"),
        },
        "entries": entries,
    }
    markdown = render_markdown(manifest)
    return manifest, markdown


def render_markdown(manifest: dict) -> str:
    lines = [
        "# A/B 与 B-only 任务分组审阅清单",
        "",
        f"> 名单冻结日期：{FROZEN_DATE}。当前仅完成分组，没有运行模型；31条Holdout未执行。",
        "",
        "## 数据来源与规则",
        "",
        "开发集由 `agent_tasks.jsonl` 的95条与 `agent_comparison_50.jsonl` 的40条组成，共135条；Holdout为21+10条，共31条。源文件未修改，SHA-256见同目录的 `agent_task_ab_partition.json`。",
        "",
        "显式证据候选按源验收中的 `checks[].kind == evidence` 识别，共52条（23+29）。其余任务逐条依据用户消息、预期结果、关键事实、验收项、fixture和适用知识文档审查；不以场景名或数据集标签作为分组理由。",
        "",
        "A/B 集合中A为单 Main Agent，B在需要政策检索时强制委派Policy Agent；Orders Agent由Main自主选择。Faithfulness设置在两组保持一致。B-only只运行B。所有开发任务未来都进入B；仅A/B集合进入配对差异分析。",
        "",
        "## Policy 与 Faithfulness 质量记录",
        "",
        "Policy证据质量按显式 `evidence` 验收逐条统计：引用须命中该项允许的文档族，且chunk必须解析到fixture索引；报告证据覆盖率和引用完整性。14条人工补入的政策推理样本没有显式来源断言，会单独标记为 `not_scored`，不混入证据覆盖率分母。",
        "Faithfulness直接记录生产Harness已有的Faithfulness Agent/Checker裁定、问题及证据引用，不额外调用裁判模型。通过、失败、证据不足、校验不可用和未触发分别统计；不可用/未触发不会计作通过或纳入已裁定通过率分母。A/B配置相同。",
        "",
        "## 数量",
        "",
        "| 项目 | 数量 |",
        "|---|---:|",
        f"| 开发任务 | {manifest['summary']['development_tasks']} |",
        f"| 显式 evidence 初筛 | {manifest['summary']['explicit_evidence_candidates']} |",
        f"| 人工规则审查新增 | {manifest['summary']['manual_policy_reasoning_additions']} |",
        f"| `ab_comparison` | {manifest['summary']['ab_comparison']} |",
        f"| `b_only` | {manifest['summary']['b_only']} |",
        f"| 未运行 Holdout | {manifest['summary']['holdout_excluded']} |",
        "",
        "### 按来源与原场景保留的开发集数量",
        "",
        "| 来源 / 场景 | A/B | B-only | 合计 |",
        "|---|---:|---:|---:|",
    ]
    source_totals: dict[str, Counter] = defaultdict(Counter)
    for entry in manifest["entries"]:
        if entry["split"] == "development":
            source_totals[entry["source_file"]][entry["group"]] += 1
    for source, values in sorted(source_totals.items()):
        lines.append(
            f"| `{source}` | {values['ab_comparison']} | {values['b_only']} | {sum(values.values())} |"
        )
    for scenario, values in manifest["summary"]["development_by_scenario_and_group"].items():
        lines.append(
            f"| `{scenario}` | {values.get('ab_comparison', 0)} | {values.get('b_only', 0)} | {sum(values.values())} |"
        )
    for heading, group_name in (
        ("A/B 配对任务", "ab_comparison"),
        ("仅运行B的任务", "b_only"),
        ("Holdout（排除，不运行）", "holdout_excluded"),
    ):
        lines.extend(
            [
                "",
                f"## {heading}",
                "",
                "| ID | 来源家族 | 原场景 | 判定依据 | 理由 |",
                "|---|---|---|---|---|",
            ]
        )
        for row in manifest["entries"]:
            if row["group"] != group_name:
                continue
            reason = row["reason"].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| `{row['id']}` | `{row['source_id']}` | `{row['scenario']}` | `{row['basis']}` | {reason} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    manifest, markdown = build()
    OUTPUT_JSON.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OUTPUT_MD.write_text(markdown, encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT_JSON.relative_to(ROOT)} and {OUTPUT_MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
