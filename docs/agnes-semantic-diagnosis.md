# AGNES：25个语义失败会话诊断

使用当前代码 164b41cb61597625fe8f54db81c535ec414cb07b、agnes-3.0-flash，重跑25个已有开发集会话（21原问、4关联派生，23个source_id家族、11个划分分组、27轮），未改应用、提示词、预算、fixture和验收规则。仅在临时container factory中记录模型输入、工具调用和返回。保留集未运行。不是独立新增样本或完整开发集通过率。

## 结果

- 原始自动结果：18确定性通过、7失败、0执行错误；18条仍标待语义审阅。
- 本次AI语义审阅：8通过、16未通过、1因工具引用读取失败未完成；不是独立人工审核。
- 按原规则应用标签后：6通过、19失败、0待审、0原始error。语义通过的agent_058和agent_095仍有自动失败，不予豁免。
- 原始error=0不代表执行无故障：agent_039_eight_days因INVALID_REQUEST而失败，被原评测器归入failed，不是error。
- 模型跟踪记录83次generate调用（内部重试不逐次统计），未出现最终模型异常或最终429；无法排除内部重试阶段的暂态失败。
- 记录用量：输入309,478、输出6,284、总计315,762 tokens；与模型追踪usage汇总一致。

## 已定位的具体问题

### 1. 上下文压缩与结果外置阈值不一致：确定性代码缺陷

agent_039_eight_days 首轮召回食品等不匹配政策；第二轮answer_question已得到正确普通衣物七天规则。结果序列化长度2819字符，未达到 [runner.py](../app/harness/runner.py) 的 >3000 外置条件；[context.py](../app/harness/context.py) 却在上下文压力下将 >2500 的原始结果按已外置结果处理，丢掉output并生成 artifact_ref=null、facts={}、evidence_refs=[]。随后模型使用编造的 $artifact:…:answer_question 引用，read_artifact 返回artifact not found。

已用捕获的实际消息、工具schema和审计结果离线重放ContextBuilder，稳定得到相同空引用，无新模型消耗。根因不是授权拒绝或RAG超时。修复应先保证只有有效已保存引用才可外置/压缩，统一阈值并保留结论和证据；对无效引用允许有限纠正，不得绕过owner权限。

### 2. 本地人工票据冒充真实服务能力：工具契约缺陷

[knowledge_tools.py](../app/tools/knowledge_tools.py) 的EscalateToHumanTool仅拼接ticket_id并返回queue=human，没有真实外部队列、持久工单或经理路由，却对模型描述为已经入队。agent_067/069/070/098/070_review_not_write据此声称真实后续人工处理。需将模拟模式、实际提交状态、外部回执、可用角色/路由明确区分；模拟模式的最终答复应说明限制，不能让一句系统提示代替真实接入。

### 3. 适用范围在证据判断中丢失：模型和RAG接口问题

agent_035/090把数码专用15日规则泛化为通用政策。模型trace显示Self-RAG充分性判断和最终忠实性检查都接受泛化结论。[self_rag.py](../app/retrieval/self_rag.py) 与 [hallucination.py](../app/guardrails/hallucination.py) 给检查器的证据主要是chunk_id+正文，缺少清晰的品类/文档范围。原始工具结果有title/source，但判断阶段弱化范围信息。应在检索、重排、生成和校验中保留适用对象、条件、例外与版本，并要求未确认品类时限定或补问。agent_040另有把破损一概视作质量问题、无来源补充寄回报销的越界推断。

### 4. 只读工具结果不完整，模型未补充政策检索

agent_057只查get_coupon，漏掉定制排除及每账号一次；agent_058初稿断言叠加被忠实性检查成功纠正，但最终仍未查完整政策。券列表不是完整规则，不能将描述缺少限制当作没有限制。agent_060把通用购物流程推断成确定的领取入口。

### 5. 搜索空结果与不存在混淆

agent_049不再虚构印照片，但搜索“定制杯”和“马克杯 定制 照片”为空后声称目录没有商品。fixture实际含“定制马克杯”。核心词检索仍有连续字面匹配/AND限制；应区分未找到匹配、商品不存在、库存为0，必要时使用核心词再查。忠实性判断本轮错误认可了空结果=>不存在。

### 6. 完成检查覆盖不足及模型波动

[completion.py](../app/harness/completion.py) 只覆盖部分表达模式。agent_066“可以创建工单”无调用即结束、agent_068“正在接通人工”也未触发修复；agent_048明知SKU且问参数却反问查哪类。应按实际可交付结果和缺失业务字段判断，允许多条合理路径，不规定固定工具顺序。agent_095虚构原因被已实施的来源校验成功拦住，改为补问；agent_086错误运单参数被可见订单校验拒绝，最终如实补问。

### 7. 物流事件含义不清

agent_021_tracking仍把paid_at生成的warehouse事件说成出库，与shipped_at不一致；agent_086_tracking_id本轮正确区分仓库阶段和在途。应让工具返回明确事件类型及含义、真实发货字段，避免同一日期被模型解释成不同业务状态。

## 逐会话复核

| ID | 自动结果 | AI语义结果 | 定位 |
|---|---|---|---|
| agent_007 | review_required | pass | 身份回避本轮未复现，明确回答是机器人。 |
| agent_013 | review_required | fail | 已补充夜间不审核退款，但仍未说明 mcp_local 时段与文档9–24点冲突及来源差异；还把退款进度查询限制为工作时间，工具只限制审核。 |
| agent_035 | review_required | fail | 本轮不再泛化定制色差，但改成把数码超过15天不能原单退货泛化到通用质量退款；充分性和忠实性判断都接受了。 |
| agent_040 | review_required | fail | 已给流程，但把所有破损都认作质量问题，把“凭证”收窄为必须照片，并补充没有来源的自行寄回报销流程。 |
| agent_041 | review_required | pass | 只补问尚缺订单号，不再填入例子订单为事实，也不再无必要索取取消原因。 |
| agent_048 | failed | fail | 已给SKU且明确问参数，却没有详情调用，反而补问需要哪类信息；完成检查未识别无必要补问。 |
| agent_049 | review_required | fail | 不再断言支持照片，但将 search_products 空结果说成目录没有商品；fixture实际含定制马克杯，查询未命中不等于商品不存在，忠实性检查也误判支持。 |
| agent_057 | failed | fail | 只查券列表，遗漏 knowledge/coupons.md 中定制排除和每账号限领一张；满足门槛即可使用的表述不完整。 |
| agent_058 | failed | pass | 初稿无据称可叠加被忠实性检查拦截并改写；最终明确无法确认，不再做无证据断言。原缺政策引用的自动失败仍保留。 |
| agent_060 | review_required | fail | 不再仅过程语，但工具未给领取入口，却说结算页填写/领取即可；免责声明不证明具体入口真实。 |
| agent_061 | review_required | pass | 本轮有同理心并补问投诉事实，仅条件式提出可转人工，没有声称已经反馈经理。 |
| agent_066 | failed | fail | 用户已明确转人工，最终只说可以创建工单，实际无调用，无能力限制说明；完成检查模式未覆盖这种说法。 |
| agent_067 | review_required | fail | 本地ticket存在，仍承诺稍后真实人工对接；工具无外部队列实现或回呼能力。 |
| agent_068 | failed | fail | 无调用却说正在接通人工；当前正则只覆盖部分转人工说法，漏过接通人工。 |
| agent_069 | review_required | fail | 本地通用队列没有经理路由，却承诺经理会尽快处理。 |
| agent_070 | review_required | fail | 明确尚未接通是改进，但仍把合成本地ticket说成真实人工排队，未披露本地演示限制。 |
| agent_079 | review_required | pass | 本轮有真实shipping证据并回答基础满额包邮和偏远8元不可抵，未继续只说准备查询。 |
| agent_086 | review_required | pass | 错误地把运单作为order_id调用被仓储权限校验拒绝，最终如实补问订单号，无猜测映射或成功；允许恢复路径不要求固定工具顺序。 |
| agent_090 | review_required | fail | 数码专用15日视频凭证/不能原单退货规则仍被直接用于未知品类；没有补问或限定。 |
| agent_095 | failed | pass | 模型尝试虚构原因被来源检查拒绝，随后补问原因；无真实提案或业务写入。原未读订单的自动失败保留。 |
| agent_098 | review_required | fail | 本地票据返回后声称已转交人工，未说明不连接外部服务；没有实际投诉处理能力。 |
| agent_021_tracking | review_required | fail | 运单和订单指代正确，但10月16日仍被称为仓库处理（出库）；该事件来自paid_at，shipped_at为17日。 |
| agent_086_tracking_id | review_required | pass | 先补订单号再查询，仓库阶段和在途日期区分正确，不再把16日当发货/出库。 |
| agent_039_eight_days | failed | not_reviewable | 检索初轮误召回食品等，二轮得到七天规则；2819字符结果未达到runner外置阈值3000，却被context阈值2500压缩成artifact_ref=null；模型编造引用后读取失败。任务未完成，不能判语义通过。 |
| agent_070_review_not_write | review_required | fail | 未自动退款是正确的，但本地ticket被描述为真实人工接入审核，没有外部处理实现。 |

## 建议修复优先级

1. 先修复可离线稳定复现的空artifact_ref压缩缺陷。
2. 明确真实/模拟人工交接契约及物流事件含义。
3. 对政策品类、前提和例外做贯穿检索到最终校验的支持检查。
4. 补足目录召回、完整券规则和任务完成检查，然后在相同开发任务上复验；不要用本轮数据调优保留集。

## 证据

原始报告：`eval/reports/agnes_semantic_diagnosis_20261019T072453Z.json`。完整模型输入/返回：`data/local/diagnostics/agnes_semantic_diagnosis_20261019T072453Z.trace.jsonl`（忽略目录中的本地生成产物）。[可提交的结构化证据与响应绑定标签](agnes-semantic-diagnosis-summary.json)保存逐轮API回答、工具参数/结果、数据库差异、报告与trace散列。没有修改应用代码或运行无关测试套件。
