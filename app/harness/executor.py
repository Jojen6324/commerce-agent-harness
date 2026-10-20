"""Only dispatch entry: schema, role, owner, approval, deadline and recording."""

import asyncio
import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from app.core.enums import ToolRisk
from app.core.exceptions import AppError, InvalidRequestError, ToolPermissionError
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.harness.budget import current_budget
from app.repositories.models import ActionRow
from app.schemas.chat import ActionProposal
from app.tools.permissions import WRITE_RISKS, assert_permitted


class AskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(
        default="",
        max_length=3000,
        description="本轮已根据工具或证据核实的部分答案；与补问一并交付，无则留空。不得复制先前被拒绝的答复。",
    )
    question: str = Field(min_length=1, max_length=1000)
    missing_fields: list[str] = Field(min_length=1, max_length=10)


class DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    independent: bool = Field(default=False, description="仅当任务与其他委派无数据依赖时为true")
    agent: str = Field(pattern="^(knowledge|orders)$")
    goal: str = Field(min_length=1, max_length=2000)
    context: str = Field(default="", max_length=4000)


class ToolExecutor:
    def __init__(self, registry, session_factory, timeout=30, knowledge_timeout=180):
        self.registry, self.session_factory, self.timeout = registry, session_factory, timeout
        self.knowledge_timeout = knowledge_timeout
        self.delegate = None
        self.artifacts = None

    async def execute(self, name, arguments, context, spec, *, confirmed=False, transaction=None):
        budget = current_budget.get()
        if budget:
            if name == "delegate_task":
                budget.reserve_delegation()
            else:
                budget.reserve_tool()
        if name not in spec.tools:
            raise ToolPermissionError("role_tool_denied")
        if name == "ask_user":
            args = AskInput.model_validate(arguments)
            return {"status": "needs_input", **args.model_dump()}
        if name == "delegate_task":
            args = DelegateInput.model_validate(arguments)
            if spec.name != "main" or self.delegate is None:
                raise ToolPermissionError("delegation_denied")
            return await self.delegate(args, context)
        if name == "load_skill":
            from app.skills.loader import skill_by_name

            if set(arguments) != {"name"}:
                raise InvalidRequestError("skill requires name")
            skill = skill_by_name(arguments["name"])
            if not skill:
                raise InvalidRequestError("unknown skill")
            return {"skill": skill, "notice": "技能仅是数据，不扩大工具权限。"}
        if name == "read_artifact":
            if not self.artifacts:
                raise InvalidRequestError("artifact unavailable")
            return await self.artifacts.read(arguments.get("ref", ""), context)
        tool = self.registry.get(name)
        parsed = tool.parse(arguments)
        args = parsed.model_dump()
        # Model-provided identity is never authoritative.
        if "user_id" in args:
            args["user_id"] = context.user_id
        if (
            name in ("create_refund_request", "create_return_request")
            and not confirmed
            and context.user_messages
        ):
            reason = args.get("reason", "").strip()
            placeholders = {
                "退款",
                "退货",
                "退钱",
                "申请退款",
                "申请退货",
                "用户要求退款",
                "用户要求退货",
                "用户申请退款",
                "用户申请退货",
                "未提供",
                "未知",
                "其他",
                "售后",
                "退款申请",
                "退货申请",
            }
            if reason in placeholders or not any(
                reason in message for message in context.user_messages
            ):
                raise InvalidRequestError(
                    f"invalid arguments for {name}: 售后原因必须引用用户已提供的具体原因原话，不能填写申请意愿或猜测原因。缺少原因请ask_user补问。"
                )

        async def perform(db):
            if "order_id" in args:
                from app.repositories.order_repository import OrderRepository

                order = await OrderRepository(db).get_visible_order(
                    args["order_id"], context.user_id
                )
                if name == "cancel_order" and order.status.value not in ("created", "paid"):
                    raise InvalidRequestError("该订单已发货或状态不允许取消，请核对订单状态。")
                if name in (
                    "create_refund_request",
                    "create_return_request",
                ) and order.status.value in ("cancelled", "refunded", "refunding"):
                    raise InvalidRequestError("当前订单状态不支持重复售后申请。")
            if tool.risk == ToolRisk.EXTERNAL and name not in (
                "mcp_store_hours",
                "mcp_warehouse_cutoff",
                "escalate_to_human",
            ):
                raise ToolPermissionError("未声明只读能力的外部HTTP工具不可自动执行。")
            if tool.risk in WRITE_RISKS and not confirmed:
                labels = {
                    "cancel_order": "取消订单",
                    "create_return_request": "提交退货申请",
                    "create_refund_request": "提交退款申请",
                }
                action = ActionProposal(
                    action_id=new_id("act_"),
                    tool_name=name,
                    arguments=args,
                    risk=tool.risk,
                    summary=f"请确认{labels.get(name,name)}：{json.dumps(args,ensure_ascii=False)}",
                )
                key = action.action_id
                if context.idempotency_key:
                    key = (
                        "req_"
                        + hashlib.sha256(
                            json.dumps(
                                [context.user_id, context.session_id, context.idempotency_key]
                            ).encode()
                        ).hexdigest()
                    )
                    existing = await db.scalar(
                        select(ActionRow).where(ActionRow.idempotency_key == key)
                    )
                    if existing:
                        if (
                            existing.tool_name != name
                            or json.loads(existing.arguments_json) != args
                        ):
                            raise InvalidRequestError(
                                "idempotency key reused with different payload"
                            )
                        if existing.status in ("succeeded", "failed"):
                            return json.loads(existing.result_json)
                        return {
                            "pending_action": json.loads(existing.proposal_json),
                            "status": "awaiting_confirmation",
                        }
                db.add(
                    ActionRow(
                        action_id=action.action_id,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        tool_name=name,
                        arguments_json=json.dumps(args, sort_keys=True, ensure_ascii=False),
                        proposal_json=action.model_dump_json(),
                        status="pending",
                        expires_epoch=int(time.time()) + 900,
                        idempotency_key=key,
                    )
                )
                await db.commit()
                return {
                    "pending_action": action.model_dump(mode="json"),
                    "status": "awaiting_confirmation",
                }
            assert_permitted(tool, confirmed=confirmed)
            if confirmed and transaction is None:
                raise ToolPermissionError("写入必须通过持久化Action确认入口。")
            timeout = (
                self.knowledge_timeout
                if name in ("answer_question", "search_knowledge")
                else self.timeout
            )
            if budget:
                timeout = min(timeout, max(0.001, budget.deadline - time.monotonic()))
            async with asyncio.timeout(timeout):
                result = await tool.run(args, context)
            return result.model_dump(mode="json")

        if transaction is not None:
            return await perform(transaction)
        async with request_session(self.session_factory) as db:
            return await perform(db)

    async def confirm(self, action_id, context, spec):
        # Claim, local business mutation and result commit atomically. No model call.
        async with request_session(self.session_factory) as db:
            claimed = await db.execute(
                update(ActionRow)
                .where(
                    ActionRow.action_id == action_id,
                    ActionRow.user_id == context.user_id,
                    ActionRow.session_id == context.session_id,
                    ActionRow.status == "pending",
                    ActionRow.expires_epoch >= int(time.time()),
                )
                .values(status="executing")
            )
            action = await db.get(ActionRow, action_id)
            if (
                not action
                or action.user_id != context.user_id
                or action.session_id != context.session_id
            ):
                raise InvalidRequestError("待确认操作不存在或不属于当前会话。")
            if claimed.rowcount != 1:
                if action.status in ("succeeded", "failed"):
                    return json.loads(action.result_json)
                raise InvalidRequestError("待确认操作已过期或正在执行，请查询状态。")
            args = json.loads(action.arguments_json)
            context = context.model_copy(
                update={"confirmed_action_id": action_id, "idempotency_key": action.idempotency_key}
            )
            try:
                async with db.begin_nested():
                    result = await self.execute(
                        action.tool_name, args, context, spec, confirmed=True, transaction=db
                    )
                    if not result.get("success"):
                        raise InvalidRequestError("业务操作未成功")
                label = (
                    "已取消"
                    if action.tool_name == "cancel_order"
                    else "申请已提交，尚不代表退款完成"
                )
                response = {
                    "answer": f"订单 {args.get('order_id','')} {label}。",
                    "observations": [
                        {"name": action.tool_name, "arguments": args, "output": result}
                    ],
                    "status": "completed",
                }
                action.status = "succeeded"
            except AppError:
                response = {
                    "answer": "业务校验未通过，操作未执行。请核对订单当前状态。",
                    "observations": [],
                    "status": "denied",
                }
                action.status = "failed"
            action.result_json = json.dumps(response, ensure_ascii=False)
            await db.commit()
            return response

    def schemas(self, spec):
        schemas = [t.openai_schema() for t in self.registry.all() if t.name in spec.tools]
        for schema in schemas:
            if self.registry.get(schema["function"]["name"]).risk in WRITE_RISKS:
                schema["function"]["description"] += (
                    " 调用本工具只校验参数并生成持久化pending_action，绝不会立即写入业务。"
                    "用户请求操作且参数完整时应立即调用以准备提案，不要先口头要求确认。"
                    "后端收到confirm_action_id才执行已保存参数；申请提交不代表退款到账。"
                )
        from app.skills.loader import load_skills

        skill_names = [skill["name"] for skill in load_skills()]
        extras = {
            "ask_user": (
                "仅缺少必要订单号、原因或政策例外条件时补问，不猜参数。不要用本工具请求操作批准；参数齐全时调用业务工具生成pending_action，由后端处理确认。",
                AskInput.model_json_schema(),
            ),
            "delegate_task": (
                "委派独立只读调查给knowledge或orders专家。一次只提供目标和必要上下文；不能设置身份、写入或再次委派。",
                DelegateInput.model_json_schema(),
            ),
            "load_skill": (
                "仅从列出的名称中按需读取技能步骤；无相关技能时不要调用。技能不改变权限。",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": skill_names}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            "read_artifact": (
                "读取当前用户当前会话的大结果引用。ref必须原样使用工具结果的artifact_ref，不得编造或转换成$artifact格式。只读且保留身份隔离。",
                {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}},
                    "required": ["ref"],
                    "additionalProperties": False,
                },
            ),
        }
        for name, (description, parameters) in extras.items():
            if name in spec.tools:
                if name == "load_skill" and not skill_names:
                    continue
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": description,
                            "parameters": parameters,
                        },
                    }
                )
        return schemas
