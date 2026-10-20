"""Shared request requirements, independent of agent mode and evaluation data.

These are evidence and authorization constraints, not tool routes. The runner
still lets the model choose how to obtain the required evidence.
"""

import re
from dataclasses import dataclass

ORDER_ID = re.compile(r"(?<![A-Za-z0-9_])ORD[A-Za-z0-9_-]+(?![A-Za-z0-9_])", re.I)
ANAPHORA = re.compile(r"这个|那个|它|这单|那单|刚才|上面|之前|同一笔|该订单")
POLICY_TOPIC = re.compile(
    r"退款|退货|退换|退钱|原路|运费|包邮|优惠券|新客券|领券|保修|质保|"
    r"发货|预售|工作时间|客服|质量问题|破损|错发|签收|售后|取消|换货|"
    r"物流|运单|包裹|拆单|拆包|多仓|寄回|换尺码|漏印|无理由|能退|退吗|"
    r"裂纹|丢件|赔钱|赔偿|补偿|礼品卡|积分|发票|开票"
)
POLICY_QUESTION = re.compile(
    r"规则|政策|规定|时效|条件|流程|怎么|怎样|如何|哪里|哪儿|地址|寄哪|"
    r"什么时候|何时|几点|多久|几天|多少(?:元|钱|天|小时|工作日|费用|年|个月)|"
    r"(?:是不是|是否|算不算).{0,8}(?:包邮|免运费|运费|无理由|保修|质保|优惠券|退款|退货|换货|赔偿|赔付|发票|开票)|"
    r"能否|能不能|可不可以|可否|能退|退吗|允许|起算|从哪|区别|建议|考虑|资格|期限|"
    r"还能.{0,8}取消|可以.{0,8}(?:退|取消|保修|寄回)|"
    r"(?:能|可以|是否).{0,8}(?:抵|免|使用|退|换|取消|申请|保修|叠加|售后|开票|赔)|"
    r"(?:能否|能不能|是不是能|是否能).{0,8}(?:确定|判断|推出|说明)|"
    r"(?:属于|算不算|是否是).{0,8}质量问题|"
    r"谁(?:来)?承担|由谁|谁出|费用|运费|无理由|补差价"
)
_HUMAN_REQUEST_ACTION = re.compile(
    r"(?:"
    r"请(?:帮我|为我|替我)?(?:现在|马上|立即|直接)?"
    r"(?:转人工|转接人工|接通人工|联系人工|找人工|安排.{0,4}(?:人工|真人|值班经理)|人工审核)"
    r"|"
    r"(?:帮我|替我|为我|给我|我要|我需要|我想找|我想要|安排|联系|转接|接通|直接|现在|马上|立即)"
    r".{0,4}(?:转人工|人工客服|人工服务|人工坐席|真人客服|真人坐席|人工处理|人工介入|人工审核|值班经理|真人)"
    r")"
)
_HUMAN_REQUEST_NEGATION = re.compile(
    r"(?:不要|别|不用|不需要|无需|不必|暂不).{0,8}"
    r"(?:转人工|人工客服|人工服务|人工坐席|真人客服|人工处理|人工介入|人工审核)"
)
_HUMAN_REQUEST_QUESTION = re.compile(
    r"(?:怎么|如何|怎样|哪里|哪儿|有没有|是否|能不能|可不可以|能否).{0,12}"
    r"(?:转人工|人工客服|人工服务|真人客服|人工处理|人工介入)"
)


def _negated_at(text, start):
    return bool(
        re.search(
            r"(?:不要|别|无需|不需要|不必|不用)[^，,。；;！？!?]{0,8}$",
            text[max(0, start - 12) : start],
        )
    )


def explicitly_requests_human_handoff(message: str) -> bool:
    """Only authorize a persistent human-service ticket on an explicit ask."""
    clauses = re.split(r"[，,。；;！？!?\n]", message)
    for clause in clauses:
        if _HUMAN_REQUEST_NEGATION.search(clause) or _HUMAN_REQUEST_QUESTION.search(clause):
            continue
        if _HUMAN_REQUEST_ACTION.search(clause):
            return True
    return False


@dataclass(frozen=True)
class RequestRequirements:
    order: bool
    policy: bool
    identified_order: bool
    action_requested: bool
    proposals_forbidden: bool

    def guidance(self) -> str:
        instructions = []
        if self.policy:
            instructions.append(
                "本轮需要核实规则、适用性或业务建议的来源。订单字段只能证明订单事实，"
                "不能替代政策证据。先通过现有只读能力取得相关原始证据，再给出规则或建议并引用；"
                "不要仅凭常识推断。已取得的成功证据可以复用，未查到适用来源则明确未知。"
            )
        if self.proposals_forbidden:
            instructions.append(
                "用户本轮仅咨询或明确禁止操作：不要调用业务写工具，也不要生成待确认提案；"
                "只完成查询、解释或必要补问。"
            )
        return "\n".join(instructions)


def request_requirements(request, *, history=None, confirmed_facts=None):
    facts = confirmed_facts or {}
    has_order_facts = bool(facts.get("last_order")) or any(
        isinstance(value, dict)
        and value.get("tool")
        in {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
        for value in (facts.get("business") or {}).values()
    )
    text = request
    if ANAPHORA.search(request):
        prior = []
        for message in (history or [])[-8:]:
            role = (
                message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            )
            content = (
                message.get("content", "")
                if isinstance(message, dict)
                else getattr(message, "content", "")
            )
            if role == "user" and isinstance(content, str):
                prior.append(content)
        text = "\n".join([*prior[-2:], request])

    # “退款进度如何” asks for a transaction state, not the refund policy.
    question = re.sub(
        r"(?:退款|退货|物流|订单)(?:状态|进度)(?:如何|怎么样|怎样|是什么)", "", request
    )
    policy = bool(POLICY_TOPIC.search(text) and POLICY_QUESTION.search(question))
    order = (
        bool(ORDER_ID.search(text))
        or bool(re.search(r"订单|物流|快递|运单|发货|签收|退款进度|退货进度|我买的|购买记录", text))
        or bool(ANAPHORA.search(request) and has_order_facts)
    )
    no_action = bool(
        re.search(
            r"(?:不要|别|暂不|无需|不需要|不必|不用).{0,8}"
            r"(?:操作|执行|申请|取消|退款|退货|提交|办理|生成)|"
            r"只给建议|只提供建议|仅给建议|不用再次申请",
            request,
        )
    )
    consultation_only = bool(
        re.search(
            r"(?:不要|别|暂不|无需|不需要|不必|不用).{0,6}(?:操作|执行)|"
            r"(?:只|仅)(?:给|提供)?建议|(?:只|仅)(?:咨询|查询|查一下|解释|说明|了解)",
            request,
        )
    )
    explicit_proposal = any(
        not _negated_at(request, match.start())
        for match in re.finditer(
            r"(?:给我|让我|准备|生成|看看|看一下).{0,10}(?:方案|提案|待确认)|"
            r"(?:方案|提案).{0,6}(?:看看|看一下)",
            request,
        )
    )
    forbids_proposal = bool(
        re.search(
            r"(?:不要|别|不需要|无需).{0,8}(?:生成|准备|创建).{0,8}(?:单|方案|提案|申请)", request
        )
    )
    action_requested = any(
        not _negated_at(request, match.start())
        and not re.search(
            r"查|核实|了解|解释|说明|建议|考虑|能否|能不能|可不可以|怎么|如何|是否|政策|规则|条件|进度|状态",
            re.split(r"[，,。！？!?；;]", request[match.start() :], maxsplit=1)[0],
        )
        for match in re.finditer(
            r"(?:帮我|替我|请(?:帮我|为我)|直接|立刻|马上|现在就)"
            r".{0,16}(?:申请|取消|退款|退货|提交|办理)|"
            r"(?:我要|我想)(?:立即|直接|马上)?(?:申请|取消|退|退款|退货|提交|办理)|"
            r"(?:^|[，。！？])请(?:取消|申请|提交|办理|退货|退款)|"
            r"^(?:取消|退款|退货|申请退款|申请退货|提交退款|提交退货)",
            request.strip(),
        )
    )
    return RequestRequirements(
        order=order,
        policy=policy,
        identified_order=bool(ORDER_ID.search(text)) or has_order_facts,
        action_requested=action_requested or explicit_proposal,
        proposals_forbidden=(
            (consultation_only or ((no_action or forbids_proposal) and not action_requested))
            and not explicit_proposal
        ),
    )
