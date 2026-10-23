# sailbill-agent · 航运智能账单助手智能体

[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![langchain](https://img.shields.io/badge/langchain-core%20%2B%20langchain--openai-1C3C3C)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-186%20passed-brightgreen)](tests)

## 一、业务场景与交付形式

航运及国际货代业务的费用信息通常分散在 **邮件、PDF 账单、扫描图片、Excel 费用表和 Word 文档** 中。
财务人员需要人工识别 Debit Note、Credit Note、Invoice 等单据中的费用项目、金额、币种、付款方式和
应收应付属性，再新增账单或更新已有账单——费时、易错、难追溯。

本系统是一个航运智能账单助手智能体：**把散落在各格式单据里的运费账单，读成财务系统能直接入账的标准 JSON**。

- 多格式解析：EML（正文 + 邮件头 + 附件递归）、PDF（文本层优先，必要时 OCR）、图片（指定 OCR API）、XLS/XLSX、DOC/DOCX；
- 要素提取与标准化：单据类型/编号/日期、交易双方、费用名称（含标准化映射）、数量/单价/金额、ISO 4217 币种、PP/CC 付款方式、小计/税额/总计；
- 账单决策：按四级规则（单据编号 / 提单号 / 订舱号 / 箱号）匹配已有账单，区分 **CREATE / UPDATE / NO_CHANGE**，只更新明确要求变更的字段；
- AR/AP 判定：以记账主体视角，结合签发方/接收方与收费方向判定应收（AR）或应付（AP），**不凭单据类型武断**；
- 风险拦截：重复提取、字段冲突、低置信度、OCR `0/O` `1/I` 混淆一律检出，无法判定时转 **PENDING_REVIEW** 人工复核，绝不虚构、绝不静默出错；
- 每条记录附带完整 `audit_trail` 处理依据，可审计、可回放。

**本地交付形式（单机版，数据不出本机）**：整个系统打包为财务人员可一键使用的桌面应用——
双击启动本地服务并自动打开浏览器，拖入单据 → 自动识别 → 待复核队列确认/驳回 → 一键导出 Excel 台账。
SQLite 账本、配置、上传文件全部保存在本机用户目录；大模型调用仅在抽取环节联网，原始单据不出本机。

> 笔试题原文完整转录与仓库约定见 **[AGENT.md](AGENT.md)**。

## 二、验收标准对照

| # | 验收要求 | 状态 | 实现与验证 |
| --- | --- | --- | --- |
| 1 | 正确解析 EML 邮件正文及附件 | ✅ | [eml.py](src/sailbill/ingest/eml.py) + [router.py](src/sailbill/ingest/router.py) 递归展开（含 .msg、图片/PDF 附件二级解析），`tests/test_ingest.py` |
| 2 | 调用 OCR API 识别 PDF 和图片 | ✅ | [ocr.py](src/sailbill/ingest/ocr.py) 按题面协议对接 `http://36.103.199.11:5002/process`，含公网文件投递层与 sha256 缓存，`tests/test_ocr_adapter.py` |
| 3 | 解析 Excel、Word 费用表格 | ✅ | [excel.py](src/sailbill/ingest/excel.py)、[word.py](src/sailbill/ingest/word.py)、[tables.py](src/sailbill/extract/tables.py)（合并单元格/跨行还原），专项测试覆盖 |
| 4 | 准确提取单据编号、费用、金额、币种 | ✅ | 规则引擎 + LLM 双通道（[rules.py](src/sailbill/extract/rules.py) / [llm.py](src/sailbill/extract/llm.py)）+ 冲突仲裁 + 证据锚定；金标评测门禁 |
| 5 | 区分新增账单和更新账单 | ✅ | [matching.py](src/sailbill/matching.py) 四级匹配 → CREATE/UPDATE/NO_CHANGE，更新仅改动有证据的字段，`tests/test_matching.py` |
| 6 | 结合记账主体判断 AR/AP | ✅ | [finance.py](src/sailbill/finance.py) 主体×双方×方向矩阵；无法判定转 PENDING_REVIEW 而非默认 AP，`tests/test_finance_validate.py` |
| 7 | 检测重复账单和数据冲突 | ✅ | [business.py](src/sailbill/validate/business.py)（重复提取/合计闭合/OCR 混淆）+ 抽取层冲突仲裁，专项测试覆盖 |
| 8 | harness 工程提升稳定性与准确性 | ✅ | 八层防线设计见下节；金标评测 + CI 门禁（[harness/](harness)） |
| 9 | 5 点以上自己的思考 | ✅ | 8 点（业务 + 技术），见下节；完整版 [docs/reflections.md](docs/reflections.md) |
| 10 | 基于 Python 和 LangChain 实现 | ✅ | `langchain-core` + `langchain-openai`（OpenAI 兼容端点，实测 DeepSeek Flash），[llm.py](src/sailbill/extract/llm.py)；编排为 LangGraph 状态机 |

**全量回归：pytest 186 个测试通过（全离线可跑）；真实单据端到端实测见第四节。**

## 三、harness 工程设计（验收 8）

核心思想：**不让 LLM 的输出直接进账本，每一层都有非 LLM 的防线**。

1. **输入侧确定性优先**：PDF 优先读文本层，每页平均字符数低于阈值才触发 OCR——OCR 是噪声源，能不用就不用；叶子文档独立成账单单元、祖先邮件只作上下文，避免多笔业务混在一个 prompt 里互相污染。
2. **归一化前置**：金额/币种/日期/主体/付款条款在进 LLM 之前由确定性代码归一（`USD 1,234.56` → `1234.56`、ISO 4217、费用名词典）——LLM 只做语义理解，不做格式转换。
3. **双通道抽取 + 交叉仲裁**：规则引擎与 LLM 独立抽取，冲突时置信度降分并记录 `[CONFLICT]` 依据，规则命中的字段优先；模型输出的每个值都要在原文锚定，定位不到就丢弃。
4. **结构化输出锁死**：LangChain 结构化输出 + Pydantic 契约，单据类型/付款方式/操作类型全部枚举约束；显式关闭 DeepSeek 思考模式以兼容 function_calling；低温度采样 + 领域术语表进 prompt。
5. **校验兜底而非静默错误**：金额合法性、币种代码、明细与合计闭合、重复提取、OCR 混淆字符逐项校验，任一阻断性校验失败 → PENDING_REVIEW 并写明原因。
6. **可解释置信度**：规则命中、双通道一致、字段完整度加权评分，每个扣分项都写入 `audit_trail`，低置信度自动降级人工。
7. **幂等与可恢复**：文件指纹幂等键防重复入账；LangGraph checkpoint 断点续跑 + 写账前人工闸门（HITL）。
8. **评测 harness 持续量化**：金标集基于真实业务单据（字段级评分、个人信息字段忽略规则、无样本自动跳过、有样本 CI 门禁），每次改 prompt/模型都能量化变好还是变坏。

## 四、项目思考（验收 9，共 8 点）

**业务上：**

1. **财务场景的第一原则不是准确率，而是"永不静默出错"**——错误金额入账是审计级代价，所以"校验不过 → 转人工"是硬规则而非建议项；系统的价值恰恰体现在它知道自己什么时候不确定。
2. **AR/AP 判定的本质不是识别问题，而是主数据问题**——同一张船公司账单对发货方是 AP、对收货方是 AR，方向不由单据决定而由"我是谁"决定；业务落地的正确顺序是先接主体/往来方主档，再谈智能化。


**技术上：**

3. **LLM 的职责边界切得越窄，准确率越高**——一切格式转换交给确定性代码，LLM 只做语义理解；这是全系统准确率的最大单一杠杆。
4. **双通道交叉仲裁比堆大模型更准、更便宜**——约九成字段是模板化的，规则引擎免费、零延迟、零幻觉；用便宜通道解决简单多数，贵模型解决难的部分。
5. **状态机的价值在可恢复性，代价是序列化纪律**——LangGraph checkpoint 支撑断点续跑与 HITL，但富对象进状态会序列化失败；纪律是状态只存原生 JSON，富对象通过 doc_id 从进程内 registry 读取。
6. **评测的可信度取决于数据治理，而不是指标公式**——合成样本会给出虚假高分，真实单据又含个人信息需要忽略规则且不能入库；评测工程的三分之二是数据治理。

## 五、真实样本实测结果

环境：DeepSeek Flash（结构化抽取）+ 题面指定 OCR API（图片与无文本层 PDF），
4 份 TestSamples 真实单据（PDF ×2、EML ×1、PNG ×1）端到端跑通，共产生 11 条记录。

**代表性实测输出（`extra` 内部字段已剔除，与题目第五节契约一致）：**

**① PDF 示例账单2.pdf（US 风格 Debit Note）——标准命中，置信度 1.00，3 条费用分账闭合：**

```json
{
  "operation": "PENDING_REVIEW",
  "document_info": {
    "document_type": "Debit Note",
    "reference_no": "DC-108478",
    "document_date": "2026-10-24"
  },
  "transaction_party": {
    "issuer": "NEPTUNE SHIPPING LIMITED",
    "recipient": "AEBO TCO IMPORT LLC"
  },
  "charge_details": {
    "charge_item": "Handling Charge",
    "amount": 65.0,
    "currency": "USD",
    "payment_term": "CC"
  },
  "financial_attributes": {
    "accounting_entity": null,
    "posting_type": null
  },
  "audit_trail": {
    "ai_reasoning": [
      "来源文件 PDF 示例账单2.pdf（pdf），共识别 3 条费用，本记录对应「HANDLING CHARGE」",
      "费用行原文：HANDLING CHARGE - 65.00 1.00 C 65.00",
      "未匹配到任何已有账单，且金额与币种通过校验，按新增账单处理",
      "无法确定应收/应付方向（记账主体未指定或与单据双方均不匹配），转人工复核",
      "未指定记账主体，无法判断 AR/AP（可用 --entity 或账单库中的 accounting_entity 指定）",
      "置信度 1.00"
    ]
  }
}
```

> 抽取本身零扣分（1.00）；转人工仅因未配置记账主体——`65.00 + 1310.40 + 35.00 = 1410.40` 与单据
> `TOTAL AMOUNT` 分账闭合。配置 `AEBO TCO IMPORT LLC` 后即输出 **CREATE ×3 / AP**。

**② PDF示例账单1.pdf（CMA CGM Arrival Notice）——双通道冲突仲裁：**

```json
{
  "operation": "PENDING_REVIEW",
  "document_info": {
    "document_type": "Arrival Notice",
    "reference_no": "CMDUCHN4053601",
    "document_date": "2026-10-26"
  },
  "transaction_party": {
    "issuer": "CMACGM & ANL",
    "recipient": "CIMC WETRANS INTL LOG CO LTD"
  },
  "charge_details": {
    "charge_item": "On Carriage",
    "amount": 245.0,
    "currency": "USD",
    "payment_term": null
  },
  "financial_attributes": {
    "accounting_entity": null,
    "posting_type": null
  },
  "audit_trail": {
    "ai_reasoning": [
      "来源文件 PDF示例账单1.pdf（pdf），共识别 1 条费用，本记录对应「On Carriage Additional - US EmOCA17」",
      "费用行原文：On Carriage Additional - US EmOCA17 USD  245.0000",
      "未匹配到任何已有账单，且金额与币种通过校验，按新增账单处理",
      "无法确定应收/应付方向（记账主体未指定或与单据双方均不匹配），转人工复核",
      "未指定记账主体，无法判断 AR/AP（可用 --entity 或账单库中的 accounting_entity 指定）",
      "置信度 0.90，扣分项：-0.10 规则引擎与模型结果存在冲突",
      "处理依据：[CONFLICT] document_type: 规则='Arrival Notice' 模型='Bill of Lading'（采用规则值）",
      "处理依据：[CONFLICT] document_date: 规则=datetime.date(2026, 10, 26) 模型=datetime.date(2026, 10, 19)（采用规则值）"
    ]
  }
}
```

> 金额 `245.00 USD` 与单据 `TOTAL CHARGES TO BE PAID` 一致；模型把单据类型认成 Bill of Lading，
> 规则通道纠正为 Arrival Notice——**冲突仲裁实录**。该单据交易方是表格列头无法可信定位，AR/AP 不判定。

**③ EML 递归附件（正文图片 InsertPic_C25B.jpg → OCR）——邮件拆分 + OCR 链路：**

```json
{
  "operation": "PENDING_REVIEW",
  "document_info": {
    "document_type": null,
    "reference_no": null,
    "document_date": "2026-10-23"
  },
  "transaction_party": {
    "issuer": "luka@pltplt.com",
    "recipient": "li"
  },
  "charge_details": {
    "charge_item": "SHA pay TAR",
    "amount": 50.0,
    "currency": "USD",
    "payment_term": null
  },
  "financial_attributes": {
    "accounting_entity": null,
    "posting_type": null
  },
  "audit_trail": {
    "ai_reasoning": [
      "来源文件 InsertPic_C25B.jpg（image），共识别 1 条费用，本记录对应「SHA pay TAR」",
      "费用行原文：SHA pay TAR：H/C $50",
      "未匹配到任何已有账单，且金额与币种通过校验，按新增账单处理",
      "无法确定应收/应付方向（记账主体未指定或与单据双方均不匹配），转人工复核",
      "未指定记账主体，无法判断 AR/AP（可用 --entity 或账单库中的 accounting_entity 指定）",
      "置信度 0.73，扣分项：-0.12 费用名 'SHA pay TAR' 未在标准词表；-0.05 [CHARGE_UNMAPPED] 费用名 'SHA pay TAR' 不在标准词表，已保留原名；-0.10 规则引擎与模型结果存在冲突",
      "处理依据：图片文件，需 OCR 识别",
      "处理依据：OCR 结果中未发现费用明细（chargeList 为空），需结合原文或人工复核"
    ]
  }
}
```

> EML 自动拆出正文图片、MBL/HBL PDF 等叶子附件并逐个解析；`H/C $50` 经 OCR + LLM 抽出，
> 非标准费用名如实保留原名并扣分——**不编造标准名**。

**④ 图片示例.png（扫描 Invoice）——阻断性校验兜底：**

```json
{
  "operation": "PENDING_REVIEW",
  "document_info": {
    "document_type": "Invoice",
    "reference_no": "QRFR1618731",
    "document_date": "2026-10-23"
  },
  "transaction_party": {
    "issuer": null,
    "recipient": null
  },
  "charge_details": {
    "charge_item": "Wharfage",
    "amount": 5.0,
    "currency": null,
    "payment_term": null
  },
  "financial_attributes": {
    "accounting_entity": null,
    "posting_type": null
  },
  "audit_trail": {
    "ai_reasoning": [
      "来源文件 图片示例.png（image），共识别 2 条费用，本记录对应「WHARFAGE ADMINISTRATION FEE」",
      "费用行原文：OCR: charge_item_raw=WHARFAGE ADMINISTRATION FEE | amount=5.00",
      "存在阻断性校验问题，转人工复核：[CURRENCY_MISSING] 第 1 条费用缺少币种",
      "未指定记账主体，无法判断 AR/AP（可用 --entity 或账单库中的 accounting_entity 指定）",
      "置信度 0.35，扣分项：-0.15 币种缺失；-0.25 [CURRENCY_MISSING] 第 1 条费用缺少币种；-0.25 [CURRENCY_MISSING] 第 2 条费用缺少币种",
      "处理依据：图片文件，需 OCR 识别",
      "处理依据：费用明细来自 OCR 结构化结果"
    ]
  }
}
```

> 扫描件里币种不可读 → **置 `null` 并阻断入账**（题面处理要求：OCR 结果不能未经校验直接入账），
> 绝不猜测币种。

**实测记录分布**：11 条记录全部正确流转——PDF 文本层直接抽取（规则通道 1.00 命中）、OCR 识别、
EML 递归拆分、双通道仲裁、校验阻断、无法判定 AR/AP 转人工，每条都携带完整处理依据。

## 六、使用方法

### 桌面单机版（推荐，财务一键使用）

```bash
uv sync
uv run sailbill-desktop          # 启动本地服务并自动打开浏览器 http://127.0.0.1:8686

# 打包成 exe 分发（可选）
uv add --dev pyinstaller
uv run pyinstaller sailbill.spec # 产物 dist/sailbill.exe，双击即用
```

1. 首次打开进入「设置」页：填 DeepSeek API Key、记账主体（不填则 AR/AP 无法判定，记录转人工）；
2. 「上传识别」拖入 EML/PDF/图片/Excel/Word（可多选）；勾选"写账前人工确认"则识别后先入复核队列；
3. 「批次记录」查看每条记录的摘要、**标准输出 JSON（一键复制）** 与处理轨迹；
4. 「待复核」对 HITL 暂停的记录确认入账 / 驳回（AI 自动转人工的记录需补全设置后重跑）；
5. 「台账导出」一键下载 Excel 台账。

数据目录：`%APPDATA%\sailbill\`（SQLite 账本、上传文件、config.json），可用 `SAILBILL_DATA_DIR` 覆盖。

### 命令行（开发 / 集成）

```bash
sailbill run TestSamples/ --entity "你的记账主体" --commit --save-bills out/bills.json  # 整目录 + 写回账单库
sailbill run bill.pdf --extended                       # 单文件，附带置信度/证据/匹配细节
sailbill run TestSamples/ --hitl --commit              # 写账前暂停，人工确认后 resume
sailbill run TestSamples/ --offline --no-llm           # 完全离线、纯规则
sailbill names TestSamples/                            # 费用名原始写法 → 标准名称
sailbill ocr bill.pdf                                  # 只看 OCR 结构化结果
sailbill eval --min-score 0.95                         # 金标评测门禁
uv run pytest                                          # 186 个测试，全离线
```

### 关键配置（`.env`，不配也能跑——无 Key 时自动走规则模式）

| 变量 | 说明 |
| --- | --- |
| `SAILBILL_LLM_API_KEY` / `_MODEL` / `_BASE_URL` | 任意 OpenAI 兼容端点（实测 `deepseek-flash`） |
| `SAILBILL_ACCOUNTING_ENTITY` | 记账主体；不配置则 AR/AP 无法判定转 PENDING_REVIEW（刻意设计） |
| `SAILBILL_OCR_ENDPOINT` | 题面 OCR 服务地址（默认 `http://36.103.199.11:5002`） |
| `SAILBILL_FILE_DELIVERY` | 本地文件 → OCR 可下载 URL 的投递策略：`auto` / `public` / `upload` / `none` |

> **OCR 硬约束**：题面 OCR 服务只接收 URL 并由服务端下载，访问不到 `localhost`——本地文件必须经
> 投递层变成公网可下载地址。赛题阶段 `upload` 走公网中转演示；**生产请换公司自建中转或对象存储**。

## 七、目录与文档

```
sailbill-agent/
├─ src/sailbill/
│  ├─ ingest/      eml · excel · word · pdf · ocr · router（含 .msg）
│  ├─ extract/     rules · tables · llm · prompts · grounding · from_ocr · lexicon
│  ├─ normalize/   amounts · currency · charges · payment · parties · dates · html
│  ├─ validate/    business（业务校验）· confidence（可解释置信度）
│  ├─ matching.py  finance.py  pipeline.py  output.py  models.py  cli.py
│  ├─ graph.py     LangGraph 状态机：extract → validate → posting → decide → commit
│  └─ server/      桌面单机版：FastAPI + SQLite + Web 工作台 + 启动器
├─ harness/        金标评测 + 指标 + CI 门禁
├─ eval/golden/    金标用例（基于 TestSamples/ 真实单据）
├─ docs/           architecture · parsing · graph · harness · reflections · adr/
└─ TestSamples/    真实单据（git-ignored，不随仓库分发）
```

数据与合规：仓库内不含任何真实客户运费/公司/密钥；真实单据不入库；密钥仅经 `.env`/环境变量提供。

## License

[MIT](LICENSE)
