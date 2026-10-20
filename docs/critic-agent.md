# Critic Agent：有界的答复审核

Main 的候选答复在发送前进入独立 Critic Agent。Critic 检查答非所问、遗漏明确子问题、内部矛盾、过度承诺、用户限制及关键事实疑点，不做措辞润色，也不默认承担全量事实核验。

默认输入仅包含当前问题、最近四条必要会话背景的有界摘录（每条最多 600 字符）、完整候选答案，以及从服务端真实 observations 生成的证据索引。索引列出真实引用、简短来源说明和已有 artifact_ref；不创建全量审核 artifact，不默认附送政策正文、业务结果、工具契约或持久事实。最近背景窗口之外的约束仍可能需要由 Main 明确带入候选答案。

Critic 只有 read_artifact 能力，且仅能读取本次索引的引用。原 ToolExecutor 和 RuntimeStore 继续执行工具预算、角色、用户和会话隔离检查。每次审核最多实际读取一次，随后关闭工具并强制输出裁定。该次读取原文保留在本审核上下文，不再经 Runner 的通用结果摘要重新变成同一引用；通用 ContextBuilder、artifact_digest 保持原实现，ContextBuilder 继续执行 32K 上下文上限。材料过大仍可能触发已有上下文预算失败。

裁定为 pass、revise、insufficient_evidence，包含精简 issues、evidence_refs、correction_guidance、evidence_checked。evidence_checked 由服务端实际读取结果决定，不信任模型自报。读取失败、显式截断/摘要不完整、仅有来源元数据等材料不得标记为已查证；没有读取时可以直接 pass，不强制引用。读取是否切中某个具体业务疑点仍需模型判断。pass 表示没有发现必须修正的问题，**不代表所有事实均已核验**。JSON 不合法或读取后继续请求工具，只允许一次输出格式重试；共享已有请求预算，不增加新预算系统。

Main 最多进行一次答复修正，修改后最多复审一次。答案和实质工具证据未变化时复用本轮已有审核结果，不再次调用 Critic。Main 只收到本次精简反馈，累计记录保留在 trace。修正期间仅保留既有只读工具及补问能力，原执行器权限也同步收窄，防止为了通过审核新增写操作、工单或委派。最后一步收窄到直接答复/必要补问；没有修正步数或复审仍不通过，使用既有 validation_failed 路径结束。

兼容保留 `_verify_faithfulness`、`FaithfulnessVerdict`、`faithfulness_verdict` 事件以及评测 `faithfulness_quality` 字段。新角色为 `critic`，通过 `review_type=critic`、`evidence_checked`、`facts_verified=false` 标明含义；旧 `fail` 输入继续兼容，新 Critic 产出使用 `revise`。未知引用、待确认写入、权限归属及退款到账状态等确定性检查继续先执行。原 RAG FaithfulnessChecker、自纠错、会话存储和审批流程未重写，评测验收与评分规则未修改。

2026-10-20 使用 Qwen1 中国节点 `qwen3.8-flash`（关闭思考）复核了先前 30 条任务运行中的两个故障点。原报告为 [qwen1_domestic_flash_A_critic_retry_20261020.json](../eval/reports/qwen1_domestic_flash_A_critic_retry_20261020.json)：30 条中 15 条通过、5 条失败、10 条待审阅；37 轮里有 33 轮得到 Critic pass，2 轮被确定性输出门拒绝（未调用 Critic），另有 2 轮未进入审核。旧报告曾把确定性拒绝记为 Faithfulness failed，现已更正归因副本 [qwen1_domestic_flash_A_critic_retry_20261020_regraded.json](../eval/reports/qwen1_domestic_flash_A_critic_retry_20261020_regraded.json)；任务结果和验收分数未变。

直接把 `collab_022` 原失败答案交给真实 Critic 后，Critic 读取一次物流事实 artifact，并正确给出 `revise`：明确指出用户问了是否有真实物流，原答案没有直接回答，并要求补充运单号。裁定和用量保存在 [qwen1_critic_omission_probe_20261020.json](../eval/reports/qwen1_critic_omission_probe_20261020.json)。随后用相同冻结任务重跑 `collab_022` 和多轮 `dialogue_006`，确定性验收 2/2 通过，Critic 3/3 正常运行，未创建任何人工工单；结果保存在 [qwen1_domestic_flash_critic_fix_probe_20261020.json](../eval/reports/qwen1_domestic_flash_critic_fix_probe_20261020.json)。这只是定向回归，不代表对全部 30 条或其他任务已完成真实模型复测，也不是全面前后性能比较。

仍有两个类型的限制需要区别处理：未知引用等确定性输出门在 Critic 之前短路，故 Critic 不会审核这类被拒答复；而 Critic pass 也不等同事实认证。当前未对全部 30 条重新运行，也未测得修复后的整体步数耗尽率或长期误判率。
