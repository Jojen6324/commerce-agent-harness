# A/B 与 B-only 任务分组审阅清单

> 名单冻结日期：2026-10-20。当前仅完成分组，没有运行模型；31条Holdout未执行。

## 数据来源与规则

开发集由 `agent_tasks.jsonl` 的95条与 `agent_comparison_50.jsonl` 的40条组成，共135条；Holdout为21+10条，共31条。源文件未修改，SHA-256见同目录的 `agent_task_ab_partition.json`。

显式证据候选按源验收中的 `checks[].kind == evidence` 识别，共52条（23+29）。其余任务逐条依据用户消息、预期结果、关键事实、验收项、fixture和适用知识文档审查；不以场景名或数据集标签作为分组理由。

A/B 集合中A为单 Main Agent，B在需要政策检索时强制委派Policy Agent；Orders Agent由Main自主选择。Faithfulness设置在两组保持一致。B-only只运行B。所有开发任务未来都进入B；仅A/B集合进入配对差异分析。

## 数量

| 项目 | 数量 |
|---|---:|
| 开发任务 | 135 |
| 显式 evidence 初筛 | 52 |
| 人工规则审查新增 | 14 |
| `ab_comparison` | 66 |
| `b_only` | 69 |
| 未运行 Holdout | 31 |

### 按来源与原场景保留的开发集数量

| 来源 / 场景 | A/B | B-only | 合计 |
|---|---:|---:|---:|
| `eval/dataset/agent_comparison_50.jsonl` | 31 | 9 | 40 |
| `eval/dataset/agent_tasks.jsonl` | 35 | 60 | 95 |
| `approval` | 0 | 2 | 2 |
| `boundary` | 0 | 10 | 10 |
| `clarification` | 2 | 13 | 15 |
| `complaint` | 1 | 2 | 3 |
| `cross_business` | 3 | 2 | 5 |
| `exception` | 0 | 4 | 4 |
| `handoff` | 0 | 6 | 6 |
| `inventory` | 0 | 2 | 2 |
| `logistics` | 0 | 1 | 1 |
| `multi_agent` | 23 | 1 | 24 |
| `multi_turn` | 10 | 8 | 18 |
| `order_query` | 0 | 2 | 2 |
| `policy` | 27 | 0 | 27 |
| `product` | 0 | 4 | 4 |
| `recommendation` | 0 | 1 | 1 |
| `simple_answer` | 0 | 11 | 11 |

## A/B 配对任务

| ID | 来源家族 | 原场景 | 判定依据 | 理由 |
|---|---|---|---|---|
| `agent_012` | `i012` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: faq, points_expiry）；此任务同时运行A与B。 |
| `agent_013` | `i013` | `policy` | `manual_policy_reasoning_review` | 客服时段回答依赖知识库；知识文档与本地服务接口的时段口径还不一致，需先核对证据再说明差异。 |
| `agent_014` | `i014` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: faq, installment_huabei）；此任务同时运行A与B。 |
| `agent_016` | `i016` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_policy）；此任务同时运行A与B。 |
| `agent_017` | `i017` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_electronics, return_policy）；此任务同时运行A与B。 |
| `agent_019` | `i019` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: pickup_return, return_policy）；此任务同时运行A与B。 |
| `agent_020` | `i020` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, shipping, shipping_remote）；此任务同时运行A与B。 |
| `agent_031` | `i031` | `policy` | `manual_policy_reasoning_review` | 退款到账时间取决于审核状态和支付渠道，任务要求解释这些条件，必须依据退款时效政策。 |
| `agent_034` | `i034` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: refund_timeline, return_policy）；此任务同时运行A与B。 |
| `agent_035` | `i035` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: pickup_return, refund_timeline, return_policy）；此任务同时运行A与B。 |
| `agent_036` | `i036` | `clarification` | `manual_policy_reasoning_review` | 服饰能否退取决于签收天数、吊牌、洗涤和二次销售状态；任务允许条件式政策解释，需核对服饰退换细则。 |
| `agent_038` | `i038` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom, return_policy）；此任务同时运行A与B。 |
| `agent_039` | `i039` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_apparel, return_electronics, return_policy）；此任务同时运行A与B。 |
| `agent_040` | `i040` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: missing_wrong_item, pickup_return, return_policy）；此任务同时运行A与B。 |
| `agent_044` | `i044` | `policy` | `manual_policy_reasoning_review` | 用户询问未发货订单的关闭方式，回答需说明取消资格与确认流程；这是店铺业务规则问题，不是订单事实查询。 |
| `agent_045` | `i045` | `policy` | `manual_policy_reasoning_review` | 用户询问刚付款能否取消，回答需适用订单状态对应的取消条件；缺少具体订单时也需要给出有依据的条件说明。 |
| `agent_056` | `i056` | `policy` | `manual_policy_reasoning_review` | 用户询问目前有哪些优惠券，答复依赖优惠券种类、门槛和限制等知识库规则。 |
| `agent_057` | `i057` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, promo_stacking, return_custom）；此任务同时运行A与B。 |
| `agent_058` | `i058` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, payment_methods, promo_stacking）；此任务同时运行A与B。 |
| `agent_059` | `i059` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, faq, payment_methods）；此任务同时运行A与B。 |
| `agent_060` | `i060` | `policy` | `manual_policy_reasoning_review` | 新客券领取涉及每账号领取次数、满减条件和适用范围，需检索优惠券规则；页面入口未记录时应明确未知。 |
| `agent_062` | `i062` | `clarification` | `manual_policy_reasoning_review` | 任务结果允许解释支付后48小时催发规则；投诉是否符合时限需对照配送政策，而非只凭用户评价。 |
| `agent_078` | `i078` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping, shipping_remote）；此任务同时运行A与B。 |
| `agent_079` | `i079` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping, shipping_remote）；此任务同时运行A与B。 |
| `agent_080` | `i080` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: live_presale, shipping）；此任务同时运行A与B。 |
| `agent_088` | `i088` | `policy` | `manual_policy_reasoning_review` | 退货地址不能从现有业务记录直接编造；答复需核对售后审核/取件政策并说明知识库没有真实仓库地址。 |
| `agent_089` | `i089` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: exchange_size, return_apparel）；此任务同时运行A与B。 |
| `agent_090` | `i090` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_electronics, return_policy, warranty）；此任务同时运行A与B。 |
| `agent_094` | `i094` | `policy` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, shipping, shipping_remote）；此任务同时运行A与B。 |
| `agent_097` | `i097` | `complaint` | `manual_policy_reasoning_review` | 涉嫌欺诈属于升级投诉，任务事实明确指向投诉分级与经理接入时限，需要依据投诉SLA并避免断言欺诈成立。 |
| `agent_032_refund` | `i032` | `multi_turn` | `manual_policy_reasoning_review` | 补齐订单与裂纹原因后，是否适用定制商品质量例外及退款/退货条件会影响待确认方案，需核对售后政策。 |
| `agent_038_return` | `i038` | `cross_business` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom, return_policy）；此任务同时运行A与B。 |
| `agent_039_eight_days` | `i039` | `cross_business` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_apparel, return_policy）；此任务同时运行A与B。 |
| `agent_057_custom_coupon` | `i057` | `cross_business` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, return_custom, shipping）；此任务同时运行A与B。 |
| `agent_095_missing_reason` | `i095` | `multi_turn` | `manual_policy_reasoning_review` | 定制杯的裂纹属于质量问题主张；生成退款提案前需区分定制商品无理由退货限制与质量问题例外。 |
| `collab_001` | `synthetic_collab_001` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_policy）；此任务同时运行A与B。 |
| `collab_002` | `synthetic_collab_002` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom, return_policy）；此任务同时运行A与B。 |
| `collab_003` | `synthetic_collab_003` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom, return_policy）；此任务同时运行A与B。 |
| `collab_004` | `synthetic_collab_004` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping）；此任务同时运行A与B。 |
| `collab_006` | `synthetic_collab_006` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping）；此任务同时运行A与B。 |
| `collab_007` | `synthetic_collab_007` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom）；此任务同时运行A与B。 |
| `collab_008` | `synthetic_collab_008` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: warranty）；此任务同时运行A与B。 |
| `collab_009` | `synthetic_collab_009` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: warranty）；此任务同时运行A与B。 |
| `collab_011` | `synthetic_collab_011` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom）；此任务同时运行A与B。 |
| `collab_012` | `synthetic_collab_012` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom）；此任务同时运行A与B。 |
| `collab_013` | `synthetic_collab_013` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: coupons, promo_stacking）；此任务同时运行A与B。 |
| `collab_016` | `synthetic_collab_016` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping）；此任务同时运行A与B。 |
| `collab_017` | `synthetic_collab_017` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: refund_timeline）；此任务同时运行A与B。 |
| `collab_018` | `synthetic_collab_018` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: refund_timeline）；此任务同时运行A与B。 |
| `collab_019` | `synthetic_collab_019` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: invoice_vat）；此任务同时运行A与B。 |
| `collab_021` | `synthetic_collab_021` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: pickup_return）；此任务同时运行A与B。 |
| `collab_022` | `synthetic_collab_022` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: logistics_exception）；此任务同时运行A与B。 |
| `collab_023` | `synthetic_collab_023` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping）；此任务同时运行A与B。 |
| `collab_024` | `synthetic_collab_024` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: gift_card）；此任务同时运行A与B。 |
| `collab_026` | `synthetic_collab_026` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: price_protect）；此任务同时运行A与B。 |
| `collab_027` | `synthetic_collab_027` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: shipping_split）；此任务同时运行A与B。 |
| `collab_028` | `synthetic_collab_028` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: missing_wrong_item, return_policy）；此任务同时运行A与B。 |
| `collab_029` | `synthetic_collab_029` | `multi_agent` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_apparel, return_policy）；此任务同时运行A与B。 |
| `dialogue_002` | `synthetic_dialogue_002` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom, return_policy）；此任务同时运行A与B。 |
| `dialogue_003` | `synthetic_dialogue_003` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: return_custom）；此任务同时运行A与B。 |
| `dialogue_004` | `synthetic_dialogue_004` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: warranty）；此任务同时运行A与B。 |
| `dialogue_006` | `synthetic_dialogue_006` | `multi_turn` | `manual_policy_reasoning_review` | 电子发票所需公司名称、税号、地址电话及开户行信息由开票规则定义；第二轮还需指出缺少税号。 |
| `dialogue_008` | `synthetic_dialogue_008` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: exchange_size）；此任务同时运行A与B。 |
| `dialogue_009` | `synthetic_dialogue_009` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: price_protect）；此任务同时运行A与B。 |
| `dialogue_012` | `synthetic_dialogue_012` | `multi_turn` | `manual_policy_reasoning_review` | ORD10002 是定制杯且用户要求仅退款不退货；裂纹质量问题能否走该路径需按定制售后和退款规则判断。 |
| `dialogue_014` | `synthetic_dialogue_014` | `multi_turn` | `explicit_evidence_check` | 源任务验收明确要求政策证据引用（evidence check: refund_timeline）；此任务同时运行A与B。 |

## 仅运行B的任务

| ID | 来源家族 | 原场景 | 判定依据 | 理由 |
|---|---|---|---|---|
| `agent_001` | `i001` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_002` | `i002` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_003` | `i003` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_004` | `i004` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_005` | `i005` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_006` | `i006` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_007` | `i007` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_008` | `i008` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_009` | `i009` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_010` | `i010` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_021` | `i021` | `order_query` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `agent_024` | `i024` | `order_query` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `agent_029` | `i029` | `logistics` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `agent_032` | `i032` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_033` | `i033` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_037` | `i037` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_041` | `i041` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_042` | `i042` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_043` | `i043` | `approval` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `agent_046` | `i046` | `product` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_047` | `i047` | `product` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_048` | `i048` | `product` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_049` | `i049` | `product` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_050` | `i050` | `clarification` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_052` | `i052` | `clarification` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_053` | `i053` | `clarification` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_055` | `i055` | `recommendation` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_061` | `i061` | `complaint` | `manual_no_policy_evidence_review` | 本题要求承认体验、澄清订单/事实或记录投诉意愿；验收不要求承诺具体SLA、补偿资格或政策结论。 |
| `agent_063` | `i063` | `clarification` | `manual_no_policy_evidence_review` | 本题要求承认体验、澄清订单/事实或记录投诉意愿；验收不要求承诺具体SLA、补偿资格或政策结论。 |
| `agent_064` | `i064` | `clarification` | `manual_no_policy_evidence_review` | 本题要求承认体验、澄清订单/事实或记录投诉意愿；验收不要求承诺具体SLA、补偿资格或政策结论。 |
| `agent_065` | `i065` | `complaint` | `manual_no_policy_evidence_review` | 本题要求承认体验、澄清订单/事实或记录投诉意愿；验收不要求承诺具体SLA、补偿资格或政策结论。 |
| `agent_066` | `i066` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_067` | `i067` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_068` | `i068` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_069` | `i069` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_070` | `i070` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_071` | `i071` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_072` | `i072` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_073` | `i073` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_074` | `i074` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_075` | `i075` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_076` | `i076` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_077` | `i077` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_086` | `i086` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_091` | `i091` | `inventory` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_092` | `i092` | `inventory` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_095` | `i095` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_096` | `i096` | `clarification` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_098` | `i098` | `handoff` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_099` | `i099` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `agent_100` | `i100` | `simple_answer` | `manual_no_policy_evidence_review` | 本题只要求礼貌回应、介绍客服身份或进行一般闲聊；成功不依赖任何店铺政策或知识库证据。 |
| `agent_021_tracking` | `i021` | `multi_turn` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `agent_024_cancel` | `i024` | `cross_business` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `agent_043_repeat_confirm` | `i043` | `approval` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `agent_043_foreign_confirm` | `i043` | `boundary` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `agent_086_tracking_id` | `i086` | `multi_turn` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |
| `agent_092_alternative` | `i092` | `multi_turn` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `agent_033_requested` | `i033` | `multi_turn` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `agent_070_review_not_write` | `i070` | `cross_business` | `manual_no_policy_evidence_review` | 请求是转人工/人工审核；验收由真实的本地排队能力或诚实说明接入限制决定，不要求解释退款资格或政策条款。 |
| `agent_076_injection_order` | `i076` | `boundary` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `collab_014` | `synthetic_collab_014` | `multi_agent` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `dialogue_001` | `synthetic_dialogue_001` | `multi_turn` | `manual_no_policy_evidence_review` | 所需结果来自用户订单的归属、状态、商品或物流记录；只需真实只读查询，不需判断政策资格。 |
| `dialogue_007` | `synthetic_dialogue_007` | `multi_turn` | `manual_no_policy_evidence_review` | 所需结果来自固定商品目录、库存或候选商品事实；没有适用的政策规则需要检索。 |
| `dialogue_011` | `synthetic_dialogue_011` | `multi_turn` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `dialogue_013` | `synthetic_dialogue_013` | `multi_turn` | `manual_no_policy_evidence_review` | 任务的判据是核实订单状态/归属、生成待确认提案、确认权限或幂等性；这些由固定数据库和 Harness 操作约束提供，不要求知识库政策证据。 |
| `exception_001` | `synthetic_exception_001` | `exception` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `exception_002` | `synthetic_exception_002` | `exception` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `exception_003` | `synthetic_exception_003` | `exception` | `manual_no_policy_evidence_review` | 任务考查隐私/身份校验、提示注入处理或客服能力边界；正确处理不需要检索店铺政策。 |
| `exception_004` | `synthetic_exception_004` | `exception` | `manual_no_policy_evidence_review` | 按任务验收，当前缺少订单号、商品/对象、退款原因或可映射标识；本轮应先补问，尚不能作政策适用判断。 |

## Holdout（排除，不运行）

| ID | 来源家族 | 原场景 | 判定依据 | 理由 |
|---|---|---|---|---|
| `agent_011` | `i011` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_015` | `i015` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_018` | `i018` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_022` | `i022` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_023` | `i023` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_025` | `i025` | `order_query` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_026` | `i026` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_027` | `i027` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_028` | `i028` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_030` | `i030` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_051` | `i051` | `recommendation` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_054` | `i054` | `recommendation` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_081` | `i081` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_082` | `i082` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_083` | `i083` | `policy` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_084` | `i084` | `order_query` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_085` | `i085` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_087` | `i087` | `clarification` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_093` | `i093` | `recommendation` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_084_foreign_order` | `i084` | `boundary` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `agent_018_category` | `i018` | `multi_turn` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_005` | `synthetic_collab_005` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_010` | `synthetic_collab_010` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_015` | `synthetic_collab_015` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_020` | `synthetic_collab_020` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_025` | `synthetic_collab_025` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `collab_030` | `synthetic_collab_030` | `multi_agent` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `dialogue_005` | `synthetic_dialogue_005` | `multi_turn` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `dialogue_010` | `synthetic_dialogue_010` | `multi_turn` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `dialogue_015` | `synthetic_dialogue_015` | `multi_turn` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
| `exception_005` | `synthetic_exception_005` | `exception` | `holdout_excluded` | 保留为原有Holdout数据；本次用户明确要求不执行，且不得将其并入开发集分组或据其调整名单。 |
