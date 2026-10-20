# Qwen 3.8 Flash 完整 A/C 开发集评测

本报告记录 2026-10-20 在修复后代码上的一次全新 A/C 对照。A 为单 Agent，C 为启用 `required_when_eligible` 委派策略的多 Agent。两组使用相同的 40 条开发集任务和 56 轮会话，评分规则与 fixture 未更改。

## 固定配置与覆盖范围

- 代码提交：`5a63af9b3b06a8b0b6e12f78fc0522b81853c338`
- 数据集：`eval/dataset/agent_comparison_50.jsonl`，SHA-256 `376f0d6889862624f848832a409b687c511a503fb2a7207bf44e41f1346630bb`
- 每组：40 条任务、56 轮；24 条 multi-agent、12 条 multi-turn、4 条 exception。只跑 development split。
- 模型：`qwen3.8-flash`；关闭思考；每次调用上下文上限 32K；主/子 Agent 每任务共享 80K token 预算。
- 凭据来自环境绑定 `QWEN1`，评测脚本只在进程内使用；未将凭据写入报告或仓库。
- 为兼容 Qwen API，本次客户端适配把关闭思考参数转为 `enable_thinking=false`。773 次实际模型请求全部 HTTP 200，服务端返回模型全部为 `qwen3.8-flash`。

## 结果

| 组别 | 通过 | 自动未通过 | 执行错误 | 待语义审核 | 模型调用 | 工具调用 | 评测记账 Token | Provider 返回 Token | 平均每轮耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A 单 Agent | 37/40 (92.5%) | 2 | 1 | 0 | 313 | 124 | 1,726,507 | 1,733,169 | 17.02 秒 |
| C 多 Agent | 34/40 (85.0%) | 2 | 3 | 1 | 460 | 213 | 1,866,161 | 1,870,750 | 25.08 秒 |

两组合计有 773 次请求、3,603,919 个 Provider 返回 Token；评测任务统计为 3,592,668 Token。A/C 的逐轮累计耗时分别为 953.2 秒和 1,404.7 秒，C 平均每轮比 A 慢约 47%。全程安全违规计数为 0。

场景结果：A 的 multi-agent 为 24/24 通过，multi-turn 为 9 通过、2 未通过、1 执行错误，exception 为 4/4 通过；C 的 multi-agent 为 21 通过、2 执行错误、1 待审，multi-turn 为 9 通过、2 未通过、1 执行错误，exception 为 4/4 通过。

逐任务配对：32 条两组都通过；`dialogue_012`、`dialogue_014` 从 A 未通过变为 C 通过；`collab_004`、`collab_018`、`collab_024`、`dialogue_004`、`dialogue_006` 从 A 通过变为 C 错误、待审或未通过；`dialogue_011` 两组都是执行错误。

## C 组委派监控

评测预期 24 个任务需要专家；C 实际对 24/24 个任务发起委派，共 48 次真实专家调用，没有漏掉应委派的任务。24 个所需专家回合中 18 个完整完成、6 个不完整；1 次后续委派因共享预算被阻止。A 组没有启用委派。C 的 213 次工具调用中，48 次是专家委派，其余 165 次为其他工具调用。

## 未通过与错误原因

- A、C 共通的 `dialogue_011` 是执行错误。用户补充 `ORD10003` 并明确要求“生成确认，不要直接执行”后，Agent 查询到订单，但 `cancel_order` 两次都返回 `InvalidRequestError`，没有生成预期的待确认提案，最终以 `RUN_BUDGET_EXHAUSTED / no_progress` 结束。此问题同时影响 A/C。
- A 的 `dialogue_012` 在首轮缺少退款原因时，模型把“只退款不退货”当成退款原因并创建待确认提案；任务要求先追问原因且不创建提案。C 首轮追问、用户给出破损原因后再生成正确提案，因此通过。
- A 的 `dialogue_014` 首轮正确读到退款状态为 `requested`。次轮用户只问到账规则，模型的回答说明当前记录不能证明退款已到账；确定性输出校验仍以 `unverified_refund_completion` 拒绝答案，重试后也没有通过 `refund_timeline` 证据引用检查。C 找到了到账时效资料，但答案触发“退款已经到账”的待审核信号；这条仍需人工语义审核，不能计作通过或失败。
- C 的 `collab_004` 因 `run_token_budget` 执行错误。orders 专家返回的长摘要缺少规范化 `facts/evidence_refs`，被判为不完整；knowledge 专家随后被预算保护阻止，主 Agent继续检索和校验后耗尽任务预算。
- C 的 `collab_024` 因 `tool_budget` 执行错误。订单专家确认数据里没有支付方式字段，knowledge 专家已返回礼品卡政策，但主 Agent在多次检索、重复订单查询和补问后耗尽工具预算。
- C 的 `dialogue_004` 第 2 轮没有调用订单查询，却声称查过 `ORD10001`，并把订单商品说成陶瓷花瓶；fixture 中该订单商品为耳机。验收因此未观察到成功的订单查询，且回答中的商品事实与 fixture 不符。
- C 的 `dialogue_006` 第 2 轮只检索发票政策，没有对用户补充的 `ORD10002` 做成功订单查询；确定性 `target_observed` 检查未通过。

## 评测文件

完整逐轮报告、逐工具记录、监控事件、请求 Token 用量和 A/C 配对结果保存在 `eval/reports/qwen38_flash_ac_20261020/full_ac.json`。同目录还保存了独立的 `A.json`、`C.json`、实时事件 `live.jsonl`、工具调用账本 `provider_calls.jsonl`、任务状态账本 `task_results.jsonl` 及日志。该目录是本地评测产物，不包含密钥。
