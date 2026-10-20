# 50 条 A/B Agent 对照集

独立数据集：`eval/dataset/agent_comparison_50.jsonl`，包含 30 条跨域（订单/商品事实 + RAG 政策）任务、15 条多轮会话、5 条业务异常场景，共 70 个请求轮次。

- **隔离性**：复用 `agent_fixtures.json` 中的固定订单、商品、退款与知识库。每题重置临时 SQLite，绝不连接真实用户数据库。固定 ID 与验证器输出可复现，不改历史 `intent_100.jsonl` 和 116 条旧任务。
- **评测方式**：同一题、模型、32K 单次上下文上限、80K 任务总预算和评分逻辑只运行 A/B 两组。A 是单 Agent；B 在主 Agent 执行前根据当前请求、历史和已确认事实判断是否需要知识检索，需要时强制委派给 Policy Agent。Orders Agent 对 B 可用，但由主 Agent 按需选择，不作强制要求。A/B 策略不读取场景标签、任务 ID 或标准答案。报告记录真实委派次数、必要 Policy Agent 完成情况，以及主/子 Agent 按角色的模型调用和 token 统计。
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

结果默认写入 `eval/reports/agent_comparison_50_ab.json`，保留 A/B 逐题逐轮结果与配对摘要。可用 `--arms A B` 运行两组，或只运行其中一组。`--monitor` 会将不含用户消息、工具参数或工具结果的 JSON Lines 进度实时写到 stderr，并把同一份监控事件保存在报告的 `monitoring.events`。B 组的 Policy Agent 预期与完成状态来自运行时策略决定，不依据数据集标签。预算事件会区分 Agent 上下文、总 token 预算与模型调用次数，并记录估算值和上限。真实比较应锁定模型、提示词、模型参数、资源预算、检索配置和数据版本，并记录每组 token、耗时、模型/工具调用及错误类型。两次先后运行可能受限流、系统负载和非确定性影响，最好交叉随机运行顺序并重测。脚本按 A→B 串行，未控制环境时间漂移，不能从一次小样本结果断言多 Agent 必然更优。

注意：**第 18 条跨域任务**使用 `refund_requested_v1` 作为真实申请中退款场景；如任务文件拆改，须同步维护这一 fixture。评测器只允许已知 fixture，不会自动扩展数据库对象。


### B 组委派策略与资源口径

B 对需要知识库证据的请求先运行 Policy Agent；订单事实由 Main Agent 直接查询，或按需委派给只读 Orders Agent。Policy Agent 是否必需只由当前请求、历史与已确认事实决定，不读取评测标签。A 始终关闭委派。

Policy Agent 使用独立上下文与只读检索工具查找政策证据，不通过完整问答工具额外生成或校验客服答案。主 Agent 接收专家状态、摘要、事实引用以及原始 observations，摘要本身不充当证据。B 中 Main Agent 的委派 Schema 只开放 Orders 角色，避免重复或绕开必需的政策检索。

主子 Agent 继续共享 80K Token、24 次模型调用、6 次实际工具额度和原有时限，每次模型上下文上限 32K。专家按剩余工作份额分配额度，保护后续专家与主 Agent；未用额度仍留在任务预算中，不增加总预算。委派协调调用另有每轮 2 次上限，A/B 共用此计数规则，实际执行始终经过原 ToolExecutor。报告同时保留全部调度尝试数、委派调度数、非委派工具调度数和预算拒绝数，避免把协调开销或拒绝尝试误称为成功业务查询。

新增 `delegation_allocation` 事件报告专家的 Token、模型与工具额度。历史 A/B/C 结果保留作基线，不覆盖，也不与新 A/B 结果混作同一组实验。

每次主/子 Agent 模型调用前，Runner 都注入服务器计算的实时剩余额度（工具、委派、包含当前调用的模型次数、Token、上下文上限和剩余步骤），扣除其他角色保留资源，不累计旧预算快照。`budget_snapshot` 监控事件记录相同数值。专家工具额度用完后收尾并结构化报告缺失项；没有取得证据或交接格式不合法时不计为专家完成。
