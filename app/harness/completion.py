"""Detect promises that cannot represent a completed API task.

Free-form final answers are checked separately from structured input requests.
Repairs stay within the runner budget and cannot repeat an unchanged issue.
"""

import re

from app.harness.requirements import request_requirements


def completion_issue(answer: str, *, main=True, observations=(), query=""):
    if not any(character.isalnum() for character in answer):
        return "empty_answer"
    handoff = any(
        o.get("output", {}).get("success")
        and o["output"].get("output", {}).get("queue") == "human"
        and o["output"]["output"].get("ticket_id")
        for o in observations
    )
    claimed_handoff = re.search(
        r"(?:我(?:会|将|来)|现在|正在|已为|马上).{0,24}"
        r"(?:转接.{0,8}(?:人工|经理)|转交人工|转人工|接通人工|创建.{0,6}工单|人工审核|反馈给.{0,4}经理)",
        answer,
    )
    unfinished_offer = not re.search(r"如果|如需|需要我|若", answer) and re.search(
        r"我可以.{0,24}(?:创建.{0,6}工单|转人工|接通人工)", answer
    )
    if main and not handoff and (claimed_handoff or unfinished_offer):
        return "missing_handoff_receipt"
    requirements = request_requirements(query)
    if (
        main
        and requirements.action_requested
        and re.search(
            r"(?:尚未|还未|缺少|未提供|没有提供|未说明).{0,24}(?:原因|订单号|SKU|信息|尺码)|"
            r"请(?:先)?(?:补充|提供|说明|告知).{0,24}(?:原因|订单号|SKU|信息|尺码)",
            answer,
        )
    ):
        return "unstructured_clarification"
    unexecuted_action_claim = re.search(
        r"(?:正在(?:为您|帮您|为你|帮你)?(?:办理|处理)?|"
        r"(?:马上|这就|即将|我来|我将)(?:为您|帮您|为你|帮你)?(?:办理|处理)?)"
        r"(?:申请|取消|退款|退货|提交)",
        answer,
    ) or re.search(r"即将.{0,30}(取消|退款|退货|提交).{0,40}(确认|是否)", answer, re.S)
    approval_language = (
        re.search(
            r"确认后(?:我|将为|会为|将执行|将提交).{0,20}(提交|执行|取消|退款|退货)",
            answer,
        )
        or re.search(
            r"(?:是否|请问是否|请|回复)[“\"\s]*(?:确认提交|确认取消|确认执行|继续[？?])",
            answer,
        )
        or (
            re.search(r"(?:我|为您|为你|现在).{0,20}(?:生成|准备).{0,15}(?:提案|待确认)", answer)
            and not re.search(
                r"(?:提供|补充|告知).{0,24}(?:后.{0,8}(?:才能|才可|再)|才能|才可|再)|"
                r"有了.{0,24}(?:才能|才可|就能|再)",
                answer,
            )
        )
    )
    if main and (
        unexecuted_action_claim
        or (
            not requirements.proposals_forbidden
            and (requirements.action_requested or not query)
            and approval_language
        )
    ):
        return "missing_action_proposal"
    if (
        len(answer) < 160
        and not re.search(r"如果|如需|需要我|可以帮", answer)
        and re.search(
            r"我(?:来|先)?(?:帮您|帮你)?查一下.{0,30}[。！!\s]*$|"
            r"^(?:好的[，,\s]*)?(?:让我|正在|我来|我先|先|马上)(?:为您|帮您|为你|帮你)?(?:查一下|查询|核对|查看).{0,60}[。！!\s]*$",
            answer,
        )
    ):
        return "unfinished_progress"
    return None


def missing_input_issue(missing_fields):
    """Reject an approval request encoded as missing input, not user data."""
    approval_markers = ("确认提交", "确认执行", "confirmation", "approval")
    if any(
        any(marker.casefold() in str(field).casefold() for marker in approval_markers)
        for field in missing_fields
    ):
        return "missing_action_proposal"
    return None


def policy_evidence_issue(query: str, observations=(), *, requirements=None):
    """Require a source lookup before answering a policy-style question.

    This is a completion guard, not an intent router: it never chooses a tool.
    It only asks the existing bounded repair turn to retrieve policy evidence.
    """
    if not (requirements or request_requirements(query)).policy:
        return None

    def has_grounding(observation):
        output = observation.get("output", {})
        name = observation.get("name")
        if name == "delegate_task":
            return output.get("status") == "completed" and any(
                chunk.get("chunk_id") and chunk.get("content") for chunk in output.get("chunks", [])
            )
        if not output.get("success"):
            return False
        data = output.get("output", {})
        if name in ("answer_question", "search_knowledge"):
            return any(
                chunk.get("chunk_id") and chunk.get("content") for chunk in data.get("chunks", [])
            )
        if name == "get_coupon":
            return bool(data.get("coupons")) or any(
                chunk.get("chunk_id") and chunk.get("content") for chunk in data.get("chunks", [])
            )
        if name == "mcp_store_hours":
            return all(key in data for key in ("weekday", "weekend", "night_refund_review"))
        if name == "mcp_warehouse_cutoff":
            return all(key in data for key in ("cutoff", "same_day_if_paid_before"))
        return False

    if any(has_grounding(observation) for observation in observations):
        return None

    return "policy_without_evidence"


def repair_guidance(issue):
    """Only repair the observed defect; never imply unrelated write authority."""
    guidance = {
        "unstructured_clarification": "缺少办理所需信息时，必须调用 ask_user 明确 missing_fields；把已核实的部分放入 answer。不能用普通文本补问后将任务标为 completed；不要丢失当前业务目标。",
        "empty_answer": "交付完整答案；简单问题可以直接回答，不必调用工具。",
        "unfinished_progress": "不要停留在稍后查询的承诺；现在查证并回答，缺少必要信息才补问。",
        "missing_handoff_receipt": "用户要求人工时，取得真实入队回执后再说明状态；无法入队则如实说明，不编造工单。",
        "missing_action_proposal": "若用户确实要求办理且参数齐全，调用对应业务工具生成待确认提案；不得用文字批准或ask_user代替服务器确认。若只是咨询，删除办理承诺并回答问题，不创建提案。",
        "policy_without_evidence": "尚缺政策原始证据。现在通过现有只读能力查证规则、范围和例外，再带原始引用回答；订单状态不能替代规则。不要申请、取消或生成业务提案来修复证据问题。",
    }
    items = [
        guidance.get(part, "依据真实工具结果补齐该缺项，不得编造。") for part in issue.split("；")
    ]
    return f"上一条回复尚未交付，待修复：{issue}。\n" + "\n".join(items)
