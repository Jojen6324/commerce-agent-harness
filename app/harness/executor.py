"""Only dispatch entry: schema, role, owner, approval, deadline and recording."""

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import InvalidRequestError, ToolPermissionError
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.harness.budget import current_budget
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

    async def execute(self, name, arguments, context, spec, *, confirmed=False):
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
        async with request_session(self.session_factory) as db:
            if "order_id" in args:
                from app.repositories.order_repository import OrderRepository

                await OrderRepository(db).get_visible_order(args["order_id"], context.user_id)
            if tool.risk in WRITE_RISKS and not confirmed:
                action = ActionProposal(
                    action_id=new_id("act_"),
                    tool_name=name,
                    arguments=args,
                    risk=tool.risk,
                    summary=f"请确认执行 {name}：{json.dumps(args,ensure_ascii=False)}",
                )
                return {
                    "pending_action": action.model_dump(mode="json"),
                    "status": "awaiting_confirmation",
                }
            assert_permitted(tool, confirmed=confirmed)
            async with asyncio.timeout(self.timeout):
                result = await tool.run(args, context)
            if confirmed:
                await db.commit()
        return result.model_dump(mode="json")

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
