"""Detect promises that cannot represent a completed API task.

This checks the assistant's claimed outcome, never classifies the user's intent
or selects a business tool. The runner may repair once within its existing budget.
"""

import re


def completion_issue(answer: str, *, missing_fields=(), main=True):
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
        or any(
            re.search(r"确认提交|确认执行|confirmation|approval", f, re.I) for f in missing_fields
        )
    ):
        return "missing_action_proposal"
    if (
        len(answer) < 160
        and not re.search(r"如果|如需|需要我|可以帮", answer)
        and re.search(
            r"我(?:来|先)?(?:帮您|帮你)?查一下.{0,30}[。！!\s]*$|"
            r"^(?:好的[，,\s]*)?(?:正在|我来|我先|先|马上)(?:为您|帮您|为你|帮你)?(?:查一下|查询|核对|查看).{0,60}[。！!\s]*$",
            answer,
        )
    ):
        return "unfinished_progress"
    return None


REPAIR_GUIDANCE = """你的上一条回复尚未形成可交付结果：{issue}。
不要只承诺稍后查询或用文字确认代替持久提案。需要操作且业务参数齐全时，调用对应业务工具；
写工具在未确认时只生成pending_action，不执行写入，后端会等待用户确认。不要用ask_user索要提交批准。
确实缺少订单号、原因等业务信息时继续补问；政策/查询应给出已核实结论与来源。
若缺能力或证据，请明确说明限制。不要编造参数、票据、提案或操作结果。"""
