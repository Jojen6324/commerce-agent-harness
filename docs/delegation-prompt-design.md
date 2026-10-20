# 委派提示词设计

提示词版本：`evidence-delegation-v3`。维护位置：`app/prompts/delegation.py`。参考资料读取日期：2026-10-20。

## 公开实现参考

- [OpenAI Agents SDK：Agent orchestration](https://github.com/openai/openai-agents-python/blob/main/docs/multi_agent.md)：manager 保留最终答复责任，通过 agents-as-tools 请求有边界的子任务；专用角色聚焦单一能力。代码可控制确定的流程、结构化结果和资源边界，独立任务才适合并行。
- [OpenAI Agents SDK：agents-as-tools 示例](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/agents_as_tools.py)：每个专家拥有独立 instructions 和清楚的工具职责说明；聚合角色负责最终结果。本项目根据运行时证据需求选择专家。
- [LangGraph Supervisor](https://github.com/langchain-ai/langgraph-supervisor-py/blob/main/README.md)：示例明确区分 research/math 角色职责；支持单独的 task_description 和自定义交接上下文。其 README 目前推荐直接用工具实现 supervisor，以控制上下文。项目沿用已有 Runner/ToolExecutor 实现工具式编排。

这些来源提供模式与设计原则；以下中文提示词结合本项目的证据、权限和审批契约编写。

## 本地契约

| 位置 | 职责 |
|---|---|
| 角色系统提示词 | 明确领域、工具范围、输入信任边界、禁止副作用、证据要求和停止条件 |
| 子任务目标 | 单独描述角色应完成的查证，不复制整个用户问题作为两个专家的共同任务 |
| 用户问题与历史 | 作为结构化参考数据交接；已有成功原始订单观察优先供政策专家判断适用范围 |
| 专家输出 | 只输出 `status / missing_fields / summary` JSON；服务端从原始工具结果生成 facts、引用和 child_run_id |
| 完成判断 | 仅针对本角色；已核实的否定状态不是查询失败，其他角色职责不计入缺项；格式不合法时按 incomplete 处理 |
| 主 Agent | 消费原始证据并承担最终答复；专家摘要仅供定位，不能替代证据；只补查缺失、过期或矛盾事实 |
| 预算快照 | 每次模型调用前注入当前可用工具、委派、模型、Token、上下文和步骤额度；使用服务器计算值，旧快照不累积 |

系统指令与交接数据分开。工具白名单、输入 Schema、身份、资源归属、任务总预算与实际写入审批由原执行器强制执行，提示词不能扩大权限。

报告记录 B 组的 `delegation_prompt_version`，保留历史版本结果。B 强制 Policy Agent 处理需要知识检索的请求，Main Agent 可按需委派 Orders。AGNES 调试用例是同一批样本在不同代码版本下的回归，不能作为独立新样本或全量成功率。提示词优化不修改数据集与确定性评分规则。
