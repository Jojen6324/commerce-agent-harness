# 重构验证状态

2026-10-20 用户授权“先用 agnes 调试”后，恢复离线回归和小样本真实模型调试。

## 当前结果

- 最终全量离线回归：378 passed；包含咨询写权限、结构化补问续办、专家权限、确认幂等、checkpoint、并行预算和 trace。
- AGNES 固定开发样本：`agent_001`、`agent_020`、`collab_003`、`dialogue_008`，共 4 任务 / 5 轮，确定性验收全部通过。`agent_020` 仍为 `review_required`，不能视作语义通过。
- 另有两种诊断会话，复用 `collab_003` fixture，不能作为独立新增样本计算业务成功率。
- 调试发现并修复：仅咨询仍可生成提案；普通文本补问导致 case 提前结束；专家结果返回后缺少证据复用提示；观察时间应记录在工具完成时。
- 真实模型续办复测：先补问原因、补充后沿用同一 case、生成方案、两次确认仅一条退货记录，六项检查通过。

完整记录见 [AGNES 调试报告](business-agent-agnes-debug.md) 和 `eval/reports/business_agnes_debug_20261020/`。

## 未验证范围

尚未运行新 Harness 完整 A/B、Holdout 或统一语义审阅；不推断总体成功率或多 Agent 优势。
上下文上限 32K、请求总预算 128K，共享模型/工具预算；Critic 默认关闭。
货币成本缺少带日期定价表，因此仅记录 Token，不估造费用。

## 可运行入口

- `pytest -q`：离线回归。
- `python -m scripts.debug_business_agents`：隔离数据库上的 AGNES 专家与续办诊断。
- `python -m eval.agent_ab --llm --provider agnes --monitor`：同题开发集 A/B；本次未执行完整对照。

保留原始评分和分组，不运行 Holdout，不从调试样本调整预期答案。
