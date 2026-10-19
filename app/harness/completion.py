"""Detect promises that cannot represent a completed API task.

Free-form final answers are checked separately from structured input requests.
Repairs stay within the runner budget and cannot repeat an unchanged issue.
"""

import re


def completion_issue(answer: str, *, main=True, observations=()):
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
    if main and (
        re.search(r"确认后(?:我|将为|会为|将执行|将提交).{0,20}(提交|执行|取消|退款|退货)", answer)
        or re.search(
            r"(?:是否|请问是否|请|回复)[“\"\s]*(?:确认提交|确认取消|确认执行|继续[？?])", answer
        )
        or re.search(r"正在(?:为您|帮您|为你|帮你).{0,20}(取消|退款|退货|提交)", answer)
        or re.search(
            r"(?:马上|这就|即将|我来|我将)(?:为您|帮您|为你|帮你)?.{0,30}(申请|取消|退款|退货|提交)",
            answer,
        )
        or re.search(r"即将.{0,30}(取消|退款|退货|提交).{0,40}(确认|是否)", answer, re.S)
        or (
            re.search(r"(?:我|为您|为你|现在).{0,20}(?:生成|准备).{0,15}(?:提案|待确认)", answer)
            and not re.search(
                r"(?:提供|补充|告知).{0,24}(?:后.{0,8}(?:才能|才可|再)|才能|才可|再)|"
                r"有了.{0,24}(?:才能|才可|就能|再)",
                answer,
            )
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


def policy_evidence_issue(query: str, observations=()):
    """Require a source lookup before answering a policy-style question.

    This is a completion guard, not an intent router: it never chooses a tool.
    It only asks the existing bounded repair turn to retrieve policy evidence.
    """
    policy_topic = re.search(
        r"退款|退货|退换|退钱|原路|运费|包邮|优惠券|新客券|领券|保修|质保|"
        r"发货|预售|工作时间|客服时间|质量问题|破损|错发|签收|售后",
        query,
    )
    policy_question = re.search(
        r"吗|呢[？?]?$|怎么|如何|哪里|在哪|什么时候|何时|多久|几天|是否|能不能|可不可以|"
        r"能否|规则|政策|时效|条件|流程|多少",
        query,
    )
    if not policy_topic or not policy_question:
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


REPAIR_GUIDANCE = """你的上一条回复尚未形成可交付结果：{issue}。
不要只承诺稍后查询或用文字确认代替持久提案。需要操作且业务参数齐全时，调用对应业务工具；
写工具在未确认时只生成pending_action，不执行写入，后端会等待用户确认。不要用ask_user索要提交批准。
确实缺少订单号、原因等业务信息时继续补问；如果用户同时询问通用政策，应先检索并回答政策，再补问执行所需信息。
政策问题若尚无成功的知识、优惠券或受信只读查询结果，先检索证据；没有适用来源时明确说明未找到，不要用常识作答。
用户明确要求人工时使用可用入队能力取得真实回执，无需先有订单号。简单回答可直接给出答案，不要只输出省略号。
若缺能力或证据，请明确说明限制。不要编造参数、票据、提案或操作结果。"""
