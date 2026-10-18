"""Only dispatch entry: schema, role, owner, approval, deadline and recording."""

import asyncio
import json
import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from app.core.enums import ToolRisk
from app.core.exceptions import AppError, InvalidRequestError, ToolPermissionError
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.harness.budget import current_budget
from app.repositories.models import ActionRow
from app.schemas.chat import ActionProposal
from app.tools.permissions import WRITE_RISKS, assert_permitted


class DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    independent: bool = Field(default=False, description="仅当任务与其他委派无数据依赖时为true")
    agent: str = Field(pattern="^(knowledge|orders)$")
    goal: str = Field(min_length=1, max_length=2000)
    context: str = Field(default="", max_length=4000)


class ToolExecutor:
    def __init__(self, registry, session_factory, timeout=30):
        self.registry, self.session_factory, self.timeout = registry, session_factory, timeout
        self.delegate = None
        self.artifacts = None

    async def execute(self, name, arguments, context, spec, *, confirmed=False, transaction=None):
        budget = current_budget.get()
        if budget:
            budget.reserve_tool()
        if name not in spec.tools:
            raise ToolPermissionError("role_tool_denied")
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
                        idempotency_key=action.action_id,
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
            async with asyncio.timeout(self.timeout):
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
        extras = {
            "delegate_task": (
                "委派独立只读调查给knowledge或orders专家。一次只提供目标和必要上下文；不能设置身份、写入或再次委派。",
                DelegateInput.model_json_schema(),
            ),
            "load_skill": (
                "按名称读取技能步骤；不改变权限。",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            "read_artifact": (
                "读取当前用户当前会话的大结果引用。只读。",
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
