# 本次 Harness 重构验证与对照

## 基线与条件

旧基线 `a1317f20dde5f81e3d2a28bc6f98ddb9b445546d`，保留为 `baseline/pre-harness-a1317f2`；开始时 162 项测试通过，无已有用户修改。新分支 `feat/lightweight-agent-harness`。

A 为旧 LangGraph；B 为新单 Agent；C 为新 Runner 开启按需专家。三组均使用云环境既有 AGNES 绑定、用户确认的 `https://apihub.agnes-ai.com/v1`、服务模型列表返回的正式 ID `agnes-3.0-flash`。原输入名 `agnes3.0flash` 不在模型列表，最初尝试返回 503；采用正式 ID 后文本、JSON object、原生工具调用均真实通过。没有切换其他付费服务。

模型、知识库、切块、512 维 BGE、Cross-Encoder、RRF、top-k 未同时替换。CPU 模型缓存修订：

- `BAAI/bge-small-zh-v1.5`: `7999e1d3359715c523056ef9478215996d62a620`。
- `BAAI/bge-reranker-base`: `2cfc18c9415c912f9d8155881c133215df768a70`。

A 使用基线 archive 独立目录执行；B/C 使用当前 checkout。演示数据库分别隔离。进程启动、模型加载未计入下表单请求耗时。请求真实并发为 1。采样期间出现过 429、503 和有限重试，因此不能把延迟变化完全归因于重构。

## 当前确定性验证

最终完整回归 **174 passed**，覆盖率 **74.94%**，满足原 70% 门槛。Python 3.12 本机通过；Python 3.11 的 CI 已保留配置，本机未另跑该版本。

同时通过 Ruff 格式/静态检查、mypy（124 个源码文件）、锁文件检查、前端 3 项测试与 TypeScript/Vite 构建。原分类器、Planner、Graph 专属测试随旧运行时清理，保留原数据集并新增行为、执行与恢复测试，不再比较意图准确率。CI 增加前端检查并采用冻结锁文件安装。

新增关键覆盖包括：

- 非法或截断模型输出不能执行工具；call_id 配对；参数纠错；重复无进展停止；共享预算及调用前压缩保留当前目标/事实。
- 专家白名单、子 Agent 不可写入/委派、结构化失败、独立任务并行及父任务取消清理。
- 会话/订单归属与跨用户 artifact 拒绝、过期确认、订单状态变更后的执行前复核。
- 相同 Action 并发/重复确认只创建一条申请；相同请求幂等键不得改变参数。
- 重启前后恢复事实与待确认操作；子进程提交写入后直接 `os._exit(0)`，恢复后重放不重复写入。该进程刻意不刷新 coverage，父进程验证提交结果。

SQLite Alembic `0001→0002→0003→0002→0003` 在独立临时数据库通过，旧订单表保留。真实 Uvicorn 启动、健康/就绪、静态页面、AGNES SSE 问候均通过；SSE 发送真实进度和最终 answer，没有将分块答案称为原生 token 流。

## 重新测量的检索结果

在保留的 **65 条** `rag_eval.jsonl` 上重新执行，不沿用 README 旧百分比。这里测量检索组件，不是整条 Agent 任务完成率。

| 检索方式 | Precision@1 | Recall@3 | MRR |
|---|---:|---:|---:|
| BM25 | 80.00% | 90.00% | 0.8556 |
| BGE | 81.54% | 94.62% | 0.8821 |
| BM25+BGE+RRF | 83.08% | 94.62% | 0.8923 |
| Hybrid+Cross-Encoder | 89.23% | 96.15% | 0.9321 |

检索器本身为各 Agent 共用；完整 KnowledgeService 还会进行改写、按需补检索和答案校验。未将这张组件表伪装为三套独立系统的端到端指标。

## A/B/C 真实小样本

固定三条输入：问候、七天无理由政策、ORD10002 状态。以下仅为样本记录，不是性能承诺。

| 请求 | A 秒 | B 秒 | C 秒 | B 模型调用 / 已返回 Token | C 模型调用 / 已返回 Token |
|---|---:|---:|---:|---:|---:|
| 你好 | 5.78 | 1.09 | 3.66 | 1 / 5127 | 1 / 5385 |
| 七天无理由退货规则 | 29.29 | 16.94 | 15.10 | 4 / 6572 | 4 / 6705 |
| ORD10002 状态 | 7.19 | 8.81 | 9.65 | 3 / 11479 | 3 / 11857 |

B/C 六个响应均完成；模型实际选择 `answer_question`、`get_order`，参数经 Schema 与所属用户校验。三条简单任务的 C 没有被强制委派，这是预期的按需行为，不构成多 Agent 收益证明。

按原文与演示仓储逐条开发核对，三组问候和订单状态均合理，政策回答覆盖主要例外；A 的政策答复遗漏了“签收次日起”与基础运费说明，因此不把九条样本一概称为完整任务成功。B/C 模型 Schema 提示有额外 Token 成本，不能据此宣称成本下降。旧版本的响应 Token 聚合不完整（工具案例甚至为 0），A 的调用次数与可比较总 Token **未测得**，未用该数值推导改进比例。

完整语义任务完成率、参数正确率全量统计、独立人工 Faithfulness 评审和生成评测集全量真实测试均**未测得**。原生成/检索数据保留，不能据这三条宣称全面无回退。

## 多轮真实演示及失败记录

没有隐藏失败：最初长链样本因 429/503 失败；调整了已验证政策答案直接返回、失败保守预算与限流等待后，暂停再跑的同一会话完成：

1. “定制马克杯超过七天但有质量问题还能退吗？”——真实检索后回答质量例外、举证和超时条件，带 `return_policy` 与 `pickup_return` 原始 chunk 引用。
2. 查询 ORD10002 的商品/状态——工具实际查询成功，但该轮最终答复曾被引用确定性检查拦截，记录为 `validation_failed`，**未算通过**。随后调整为接受实际调用过的工具名称来源并保留未知引用拒绝；该具体长链案例未全量重跑。独立 A/B/C 中的订单状态均已实际返回。
3. “这笔订单有质量问题，我想退货”——模型补问质量问题表现，说明多轮上下文已参与后续决策。
4. “原因是收到商品有裂纹，需要提交退货申请”——生成 ORD10002、原因“收到商品有裂纹”的持久化提案，未立即写入。
5. 相同 user/session 携带 action_id 确认——约 17ms 完成本地申请，模型调用 **0**，返回“申请已提交，尚不代表退款完成”。

另做真实双专家探测：主 Agent 发起两条 `delegate_task`；订单专家完成只读调查，知识专家遇到 429，主任务最终被共享预算停止（17 次模型尝试、6 次工具调用）。这次**不是完整成功的双专家任务**；独立只读并行、隔离和失败/取消路径通过模拟测试。未通过扩大预算掩盖限流，未据此宣称多 Agent 优于单 Agent。

## 原意图集的行为回归

保留 100 条源数据，改为允许多条合理工具能力/补问路径的行为评测。在确定性离线演示模式跑满 100 条，能力匹配为 **65/100**。这反映有限规则演示的覆盖面，**不是 AGNES 的工具选择准确率、任务完成率或新的意图分类准确率**。真实模型仅先跑小样本，没有自动扩展到整套付费生成测试。

## 复现与产物

命令与源码导读见 [harness.md](harness.md)。生成的逐例 JSON 位于忽略的 `eval/reports/`：`compare_A.json`、`compare_B.json`、`compare_C.json`、`harness_live_multi_retry.json`、`harness_live_specialists.json` 及带时间戳的检索/行为报告。文件不含密钥，保留了失败运行用于核对；它们是当前实例产物，不承诺已发布或新任务恢复。

```bash
# 当前实现，需已安装与预热模型
.venv/bin/python -m scripts.harness_compare --variant B --output eval/reports/compare_B.json
.venv/bin/python -m scripts.harness_compare --variant C --output eval/reports/compare_C.json
# 显式真实调用；先确保服务配额可用
.venv/bin/python -m scripts.harness_live_smoke --live --specialists
.venv/bin/python -m scripts.harness_live_smoke --live --specialists --specialist-probe
```

A 从基线 archive 复现：创建 `/tmp/harness-baseline`，执行 `git archive baseline/pre-harness-a1317f2 | tar -x -C /tmp/harness-baseline`；在该目录按其旧锁文件安装独立 venv（含 LangGraph），通过 `PYTHONPATH=/tmp/harness-baseline` 运行新分支 `scripts/harness_compare.py --variant A ...`。共享同一 HF 缓存与 AGNES 绑定，不创建 worktree，不向 archive 写入密钥。

## 仍有边界

生产认证、跨进程会话锁、标准 MCP 客户端、真实支付/Java、任意位置任务恢复和完整真实模型质量评估不在已验证能力内。当前适合一个 worker 的本地开发与演示。限流下任务会停在有边界的失败/补问，而不是虚构业务成功。
