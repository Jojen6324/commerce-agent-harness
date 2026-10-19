# 50 条单 Agent / 多 Agent 对照集

独立数据集：`eval/dataset/agent_comparison_50.jsonl`，包含 30 条跨域（订单/商品事实 + RAG 政策）任务、15 条多轮会话、5 条业务异常场景，共 70 个请求轮次。

- **隔离性**：复用 `agent_fixtures.json` 中的固定订单、商品、退款与知识库。每题重置临时 SQLite，绝不连接真实用户数据库。固定 ID 与验证器输出可复现，不改历史 `intent_100.jsonl` 和 116 条旧任务。
- **评测方式**：同题分别以 `agent_delegation_enabled=False`（单 Agent）与 `True`（主 Agent 可将只读任务委派给 knowledge/orders 专家）运行。**不强制模型委派**；开启委派不等于实际发生多智能体协作。
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

结果写入 `eval/reports/agent_comparison_50_ab.json`，保留两组逐题/逐轮结果与配对摘要。`--monitor` 会将不含用户消息、工具参数或工具结果的 JSON Lines 进度写到 stderr，并把同一份监控事件保存在报告的 `monitoring.events`。预算事件会区分 Agent 上下文、总 token 预算与模型调用次数，并记录估算值和上限。真实比较应锁定 AGNES 模型、提示词、模型参数、资源预算、检索配置和数据版本，并记录每组 token、耗时、模型/工具调用及错误类型。两次先后运行可能受限流、系统负载和非确定性影响，最好交叉随机运行顺序并重测。当前脚本按 A→B 串行，未控制环境时间漂移，不能从一次小样本结果断言多 Agent 必然更优。

注意：**第 18 条跨域任务**使用 `refund_requested_v1` 作为真实申请中退款场景；如任务文件拆改，须同步维护这一 fixture。评测器只允许已知 fixture，不会自动扩展数据库对象。
