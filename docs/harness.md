# 轻量电商客服 Agent Harness

## 启动与配置

在 `/workspace/commerce-agent-harness` 使用已有 checkout，不创建额外 worktree。

```bash
export HF_HOME=/workspace/.cache/huggingface
export HF_ENDPOINT=https://huggingface.co
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
uv sync --frozen --extra dev --extra rerank --python 3.12
.venv/bin/python -m scripts.warm_models
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m uvicorn app.asgi:app --host 127.0.0.1 --port 8000
```

前端开发另开终端，在 `frontend` 执行 `npm run dev`。Vite 代理 `/api`。
模型 API 使用已有云环境 `AGNES` 密钥绑定；不要将其复制到文件、日志、Git 或聊天。
`AGNES_BASE_URL=https://apihub.agnes-ai.com/v1`，正式模型 ID 为 `agnes-3.0-flash`，Bearer 认证。
已真实验证文本、JSON object 和原生 function tool calling。无 AGNES 时进入确定性离线演示，不切换其他付费服务。
没有通用的自然语言动作解析器，也没有自动 JSON 兼容模式：截断、非法 JSON、缺参数均不能执行。

模型调用的上下文硬上限为 32K（包含工具 Schema 和输出预留），主/子 Agent、检索、Faithfulness 审核和修正共享任务级 Token、模型调用、工具调用与 deadline 预算。Settings 默认任务预算为 80K Token、24 次模型调用、300 秒；环境变量可收紧这些值。普通工具默认 30 秒，知识检索默认 180 秒，主 Agent 默认最多 8 步、6 次工具调用。
Token 估计是 CJK 字符启发式，不能替代供应商精确 tokenizer；不能安全放下关键上下文时停止。
`token_usage` 仅累计已返回的 usage，失败请求保守预留单独列在 `diagnostics.reserved_tokens_without_usage`，不能当作已计费 Token。

原 BGE 模型（512 维）、Reranker、知识库、切块、RRF 和 top-k 保持不变。CPU PyTorch 通过官方索引安装，避免下载无用 CUDA 依赖。
模型缓存固定在 `/workspace/.cache/huggingface`。`scripts/warm_models.py` 记录已验证的模型修订；运行时默认仍用原模型 ID。
PDF/Office 的 Unstructured extras、Postgres、Redis、Qdrant 是可选能力，本次未将它们作为闭环前置条件。

## 执行与源码导读

1. `services/chat_service.py`：校验会话归属、加载历史与摘要、结构化事实和待确认 Action；同会话并发明确返回冲突。模型调用期间没有业务长事务。
2. `harness/runner.py`：唯一的模型—工具循环。`AgentSpec` 控制角色与白名单。普通文本结束；`ask_user` 补问；工具观察回填；`delegate_task` 委派；写工具生成确认提案。没有全局 intent classifier、固定 Planner→Executor 或 LangGraph。
3. `harness/executor.py`：所有业务、专家、Skill、HTTP 代理和确认执行统一经过 Schema、角色、身份、资源归属、风险、超时检查。身份来自服务端 Context，不接受模型选择用户。参数错误可有限修正；权限拒绝终止；模型无法调用隐藏写入入口。
4. `services/knowledge.py` 和 `tools/knowledge_service.py`：运行时 `search_knowledge` 只做一次 BM25+BGE 混合召回、RRF、去重、精排和缓存读写，返回带原文、来源、版本、品类、限制、document_id、chunk_id 的证据。它不调用生成模型改写 query、判定充分性或生成答案；需要补检索时由 Agent 发起下一次显式工具调用。`answer_question` 只保留为非生产兼容 helper，不在主 Agent 工具集中。
5. `harness/validation.py` 与 `ChatService._verify_faithfulness`：先检查未知引用、退款/取消等完成声明和服务端回执；一般的、有工具证据的候选答案再交给只读 Faithfulness Agent 核对。审核结果引用必须来自真实 chunk 或工具观察；审核失败的反馈回到 Main Agent，最多修正两次，审核不可用时 fail closed。准确的服务端业务回执、无工具观察的闲聊等确定性路径不重复调用模型裁决。
6. `harness/context.py`、`harness/budget.py`：每次主/子模型调用前准备上下文；大结果外置、保留受控引用和结构化事实、压缩早期历史。保留 tool_call_id 配对、当前目标和受保护事实。历史/摘要在后续轮次真实加载。RAG、校验和专家共享 ContextVar 预算；失败保守保留额度。
7. `harness/delegation_policy.py` 和 `harness/delegation.py`：`OFF / AUTO / REQUIRED_WHEN_ELIGIBLE` 只依据当前请求、历史和已确认事实选择专家，不读评测标签。Policy 角色只拥有 `search_knowledge`、`read_artifact`，结构化结论经 Pydantic 校验，引用逐一对照成功检索观察；最多三条不同政策 query。订单专家仅拥有白名单只读业务工具。所有角色复用 `AgentRunner`，写操作只在 Main Agent 的原权限内，专家不能写入或继续委派。
8. `repositories/runtime.py`、`repositories/models.py`：持久化可信会话事实、来源 run/trace、Artifact、Run 事件与 Action。Trace ID 贯穿 root 与子 run，`parent_run_id` 显式关联政策和 Faithfulness 子任务。Artifact、Run、Session 均校验 user/session 所属；临时子 Agent 对话不追加到用户消息历史。事件不保存隐藏推理或认证头。
9. `ToolExecutor.confirm`：通过 Action 的归属、TTL、条件更新 claim、服务器保存参数再次校验；本地写入与 Action 成功结果同事务提交。相同 Action 并发/重试返回持久化结果。模型不会重新规划确认参数。

Skills 保留现有 JSON 定义，只注入短目录，由 `load_skill` 按需加载；不能扩大工具权限，本次未生成或修改业务 Skill。
MCP 名称保留兼容，但现有实现是本地 handler 和自定义 HTTP `{name, arguments}` POST，**不是标准 MCP JSON-RPC、发现或官方 SDK 客户端**。未声明只读能力的远程工具默认拒绝自动执行。没有额外付费服务。

## API 与进度

保留 `/api/v1/chat`、`/api/v1/chat/stream`、`/api/v1/health`、`/api/v1/ready`。
主要 JSON 字段保留；`intent`、`complexity`、全局 `strategy` 可为 null，不再代表路由。
新增 `run_id`、`status`、`diagnostics`。SSE 依次传执行进度（tool/delegation/compression/validation/needs_input/needs_confirmation），最终答案校验后才发送 answer，最后 done。它不是模型原生 token 流。
前端保留原 UI、引用与确认卡片，只增加进度提示和错误处理。

## 三个演示

首次演示前显式初始化本地样例（只在需要时，种子支持重复执行；不要重建用户数据库）：

```bash
.venv/bin/python - <<'PY'
import asyncio
from app.config.settings import get_settings
from app.services.container import build_container
async def main():
    c=await build_container(get_settings(),seed=True)
    await c.aclose()
asyncio.run(main())
PY
```

1. **政策问答与引用**：发送“定制马克杯超过七天但有质量问题还能退吗？”。预期核对定制及质量例外，返回政策引用，不编造订单事实。
2. **多轮订单查询**：先“查询订单 ORD10002 的商品和状态”，随后携带返回的 session_id 发送“这笔订单的物流呢？”。从历史和结构化事实解析指代，并再次查询当前业务数据。
3. **政策与业务合并到确认**：先问“帮我处理退货”，缺订单号应补问；提供 ORD10002，按需说明质量问题或无理由；核对定制商品与时效政策。明确请求“收到的马克杯有裂纹，原因是质量问题，请提交退货申请”。返回 pending_action 后，同一 user_id/session_id 以 `confirm_action_id` 明确确认。返回 `requested` 的本地退货申请结果，不能宣称退款已完成。改变参数须重新生成 Action；同一 action_id 始终执行已确认的参数。

真实模型演示（主动发起请求、串行，可能遇到供应商限流）：

```bash
.venv/bin/python -m scripts.harness_live_smoke --live --specialists
```

离线模式是有限确定性演示，不声称具备模型的完整理解能力。行为与审批闭环由受控模型模拟测试完整覆盖。

## 验证、评测与回退

```bash
.venv/bin/python -m pytest -q --cov=app --cov=eval --cov-fail-under=70
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
(cd frontend && npm test && npm run build)
.venv/bin/python -m eval.runner --task retrieval
.venv/bin/python -m eval.runner --task behavior --split development --full
# 主动真实调用小样本，不默认跑满集
.venv/bin/python -m eval.runner --task behavior --llm --limit 5
```

保留原 intent_100、rag_eval、e2e_eval 数据集；原 intent 标签仅用于离线定义允许能力，不参与运行时推理，不再计算分类准确率。行为能力命中不是语义任务完成率。

基线：`baseline/pre-harness-a1317f2`。回退可在确认工作区干净后 `git switch baseline/pre-harness-a1317f2` 并按旧锁文件重新同步；不要 reset 用户改动。旧代码没有持久化 Action 能力，无法消费新 Action。业务旧表兼容，新 harness 表可保留。恢复新分支后再同步新锁文件。
新增 Alembic 0003 为增量表；本地仍沿用原 create_all 启动约定。需要迁移的部署应先在备份数据库演练升级/回滚，不能自动 downgrade 删除用户 Action。

## 明确边界

- 当前 `user_id` 是本地演示身份输入，尚无可信公网认证；对外部署须绑定真实认证身份。模型无法更改 Context 中的身份，但 API 调用方仍必须可信。
- 同会话冲突锁为单进程策略；按一个 Uvicorn worker 运行。未提供跨机器 session lease 或分布式任务恢复。
- 支持历史、摘要、事实、待确认 Action 和已完成写入结果恢复；只读可重试。运行中进程意外退出时事件可能停留 running，需要依据持久化结果重新发起；不承诺任意指令位置恢复或 exactly-once。
- 本地订单/物流与申请是演示仓储，不连接 Java、支付网关或真实承运商。本次未实现或修改 Java。
- 模型 Token 估计、模型裁判和小样本人工抽查有局限。没有独立人工双盲评审或完整生产恢复证明。真实验证范围见对照报告。

## 参考来源与许可

仅参考机制，未复制两项目代码，未整套引入其框架：

- OpenCode `802f2d8e75098ae0b45ad839ce98a31589ea8102`（MIT）：阅读 [`packages/core/src/agent.ts`](https://github.com/anomalyco/opencode/blob/802f2d8e75098ae0b45ad839ce98a31589ea8102/packages/core/src/agent.ts) 与 [`tool/plugin/subagent.ts`](https://github.com/anomalyco/opencode/blob/802f2d8e75098ae0b45ad839ce98a31589ea8102/packages/core/src/tool/plugin/subagent.ts)，参考主/子角色隔离、工具许可边界和子会话结果汇总。
- Hermes Agent `22063fc099ec9ad8357d7f8f6e6248a0400680d3`（MIT）：阅读 [`agent/context_compressor.py`](https://github.com/NousResearch/hermes-agent/blob/22063fc099ec9ad8357d7f8f6e6248a0400680d3/agent/context_compressor.py) 与 `context_compressor_prellm.py`，参考调用前压缩、保护首尾及确定性降级。本实现不调用辅助付费模型，也不复制其多供应商 fallback 系统。


新的任务语料、逐题人工审阅目录、fixture与分组协议见 [任务行为评测](agent-task-evaluation.md)。本次架构的执行流程、Trace 样例、回归状态和已知限制见 [Harness 架构改造验收](harness-architecture-v2.md)。原 intent 工具命中指标已移除；语义待审阅与实际通过分开计数，保留集必须显式选择。


## 提案、结束状态与慢环境配置

用户明确请求业务操作且参数齐全时，主Agent调用写工具以生成持久化`pending_action`；这一步不改变业务状态。`ask_user`只补业务信息，不代替提案或索要操作批准。只有后续携带`confirm_action_id`的请求执行保存参数，确认与重试不重新调用模型。针对只承诺“马上处理”“查一下”或口头要求批准而没有提案的回复，Runner最多纠正一次，仍未交付则返回`validation_failed`；纠正不增加角色权限，不选择固定业务工具，不猜参数。

退款/退货原因必须引用当前或历史用户消息中的具体原因原话，不能用“用户要求退款”等意愿充数。ChatService把可信用户轮次填入Context，执行器在准备新提案时检查原因来源；不读取助手摘要作为原因证明。确认阶段重放已保存参数。内部直接调用执行器的代码若没有用户轮次Context，仍须由调用方保证参数来源。

商品查询优先原词匹配；无匹配时支持空格、斜线、逗号分隔的多个关键词同时匹配。每词仍保留SQL LIKE转义，不因组合搜索扩大通配符权限。

慢云环境可配置`AGENT_KNOWLEDGE_TIMEOUT`（默认180秒），只影响`answer_question`与`search_knowledge`。`AGENT_TOOL_TIMEOUT`仍为30秒；`AGENT_DEADLINE_SECONDS`默认300秒。RAG和子Agent不能越过剩余父级deadline，Token、模型次数、工具次数限制保持不变。此配置适应较慢环境，不代表已确认超时根因。

HTTP 200只表示API正常返回结构。调用方需检查`status`、`error_code`、`diagnostics.failure`与`events`，区分`completed`、`needs_input`、`awaiting_confirmation`、`failed`、`budget_exhausted`、`validation_failed`和`denied`。错误记录包含安全的错误码、原因和可重试性；不会返回认证头或密钥。外层deadline也保留run_id和终止状态。校验器不可用时不能声称“未配置模型”或把未校验答复标记为已校验；生成降级会保留原因，资料摘录限于一个完整来源并附原引用。

本轮未修改任务语料、划分或语义评测规则。验证结果见 [任务执行优化与验证](agent-optimization-results.md)。
