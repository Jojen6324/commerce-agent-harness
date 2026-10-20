# 50 条单 Agent / 多 Agent 对照集

独立数据集：`eval/dataset/agent_comparison_50.jsonl`，包含 30 条跨域（订单/商品事实 + RAG 政策）任务、15 条多轮会话、5 条业务异常场景，共 70 个请求轮次。

- **隔离性**：复用 `agent_fixtures.json` 中的固定订单、商品、退款与知识库。每题重置临时 SQLite，绝不连接真实用户数据库。固定 ID 与验证器输出可复现，不改历史 `intent_100.jsonl` 和 116 条旧任务。
- **评测方式**：同一题、模型、32K 单次上下文上限、80K 任务总预算和评分逻辑运行 A/B/C 三组。A 关闭委派；B 保留原有模型自主调用只读专家；C 在主 Agent 执行前根据当前请求、历史和已确认事实选择必要专家，并通过原 `ToolExecutor` 调用。C 策略不读取场景标签、任务 ID 或标准答案。报告分别记录真实委派次数、必要专家完成情况，以及主/子 Agent 按角色的模型调用和 token 统计。
- **判分**：使用既有 `grade_turn`，综合用户输入、参数/工具返回、知识引用、数据库变化与审批边界。纯关键词检查不足以确认语义任务完成；须审阅逐轮响应，对同义表达假阴性人工复核。
- **分组**：每 5 题保留 1 题 holdout，共 development 40 / holdout 10。holdout 是合成集内部留出，不代表统计独立，勿调参后再次声称未见过。
- **异常覆盖**：越权订单、无效订单、伪系统指令、退款条件缺失、SKU/订单号混淆。**不包括**真正的 429、超时或依赖宕机故障注入，后者需独立 mock/代理故障测试。

## 使用

```bash
# 离线仅验证管线，不能当作真实模型准确率
python -m scripts.compare_agent_benchmark --split development --limit 3

# 开启真实模型（需要 AGNES 环境配置，可能遇到限流/耗费配额）
python -m scripts.compare_agent_benchmark --split development --live --limit 5
python -m scripts.compare_agent_benchmark --split all --live

# 实时观察任务、Agent、模型 token/耗时、上下文预算、工具和校验事件
python -m scripts.compare_agent_benchmark --split development --limit 3 --live --monitor

# 显式使用 DeepSeek Flash，并关闭思考模式
DEEPSEEK_MODEL=deepseek-flash python -m scripts.compare_agent_benchmark --split development --limit 3 --live --provider deepseek --monitor
```

结果默认写入 `eval/reports/agent_comparison_50_abc.json`，保留各组逐题/逐轮结果与配对摘要。可用 `--arms A B` 或 `--arms C` 选择运行组。`--monitor` 会将不含用户消息、工具参数或工具结果的 JSON Lines 进度实时写到 stderr，并把同一份监控事件保存在报告的 `monitoring.events`。B 组是否预期委派仅用于报告分组；C 组的预期专家来自运行时策略决定。预算事件会区分 Agent 上下文、总 token 预算与模型调用次数，并记录估算值和上限。真实比较应锁定模型、提示词、模型参数、资源预算、检索配置和数据版本，并记录每组 token、耗时、模型/工具调用及错误类型。两次先后运行可能受限流、系统负载和非确定性影响，最好交叉随机运行顺序并重测。脚本按 A→B→C 串行，未控制环境时间漂移，不能从一次小样本结果断言多 Agent 必然更优。

注意：**第 18 条跨域任务**使用 `refund_requested_v1` 作为真实申请中退款场景；如任务文件拆改，须同步维护这一 fixture。评测器只允许已知 fixture，不会自动扩展数据库对象。


### C 模式委派编排与资源口径

C 仅对已确定订单、同时需要订单事实与政策证据的跨域请求执行预检。简单订单状态、纯政策问题、缺订单身份的请求由主 Agent 直接处理或补问。策略只读取请求、历史和已确认事实，不读取评测标签。

预检先由订单专家查证，再把成功的原始观察交给知识专家判断适用政策；这里有数据依赖，因此顺序执行。每位专家接收角色限定目标与独立模型上下文。知识专家只使用证据检索，不再通过完整问答工具额外生成、校验最终客服答案。主 Agent 接收专家状态、摘要、事实引用以及原始 observations，摘要本身不充当证据；预检后不再重复委派。

主子 Agent 继续共享 80K Token、24 次模型调用、6 次实际工具额度和原有时限，每次模型上下文上限 32K。专家按剩余工作份额分配额度，保护后续专家与主 Agent；未用额度仍留在任务预算中，不增加总预算。委派协调调用另有每轮 2 次上限，A/B/C 共用此计数规则，实际执行始终经过原 ToolExecutor。报告同时保留全部调度尝试数、委派调度数、非委派工具调度数和预算拒绝数，避免把协调开销或拒绝尝试误称为成功业务查询。

新增 `delegation_allocation` 事件报告专家的 Token、模型与工具额度。新计数规则与旧报告不同，历史 A/B/C 结果保留作基线，不覆盖，也不直接用旧版调度总数声称性能提升。

每次主/子 Agent 模型调用前，Runner 都注入服务器计算的实时剩余额度（工具、委派、包含当前调用的模型次数、Token、上下文上限和剩余步骤），扣除其他角色保留资源，不累计旧预算快照。`budget_snapshot` 监控事件记录相同数值。专家工具额度用完后收尾并结构化报告缺失项；没有取得证据或交接格式不合法时不计为专家完成。
