# AGENT.md · 题目原文与仓库约定


---

# AI agent 笔试题

## 多格式文档解析的航运智能账单助手智能体

## 一、项目背景

航运及国际货代业务的费用信息通常分散在邮件、PDF 账单、扫描图片、Excel 费用表和 Word 文档中。财务人员需要人工识别 Debit Note、Credit Note、Invoice 等单据中的费用项目、金额、币种、付款方式和应收应付属性，再新增账单或更新已有账单。

本项目要求开发一个航运智能账单助手智能体，通过多格式文档解析、OCR 识别和大模型语义分析，
自动提取费用信息，匹配已有账单，执行新增或增量更新，并输出可供财务系统使用的标准结构化数据。

## 二、项目目标

系统需要实现：

1. 支持 EML、PDF、图片、Excel、DOC/DOCX 等数据源；
2. 自动解析 EML 邮件正文、邮件头及附件；
3. PDF 或图片通过指定 OCR API 识别；
4. 从识别结果中提取航运账单和费用明细；
5. 对费用名称、币种和付款方式进行标准化；
6. 根据输入信息新增账单或更新已有账单；
7. 根据记账主体和交易方向判断 AR/AP；
8. 识别重复账单、冲突数据和低置信度结果；
9. 记录文件来源、原始文本和处理依据；
10. 输出符合约定格式的 JSON 数据。

**必须使用语言及框架**：python，langchain

**交付物**：代码 zip 或提供 Git 仓库链接

## 三、输入数据

系统支持以下输入：

- EML 邮件；
- PDF 文本或扫描账单；
- JPG、PNG、TIFF 等图片；
- XLS/XLSX Excel 文件；
- DOC/DOCX Word 文件；
- 已存在的账单 JSON，用于账单匹配和更新。


PDF 和图片通过以下 OCR API 识别：

```python
import requests

file_url = "https://example.com/bill.pdf"

url = "http://36.103.199.11:5002/process"

data = {
    "file": file_url,
    "task": "orderChange",
    "callBack": "",
    "taskType": ""
}

response = requests.post(url, json=data, timeout=(10, 300))
response.raise_for_status()
ocr_result = response.json()
```

`file` 应为 OCR 服务端能够访问的 PDF 或图片下载地址。

## 四、核心功能

### 1. 多格式文档解析

- EML：解析主题、发件人、收件人、正文及附件；
- PDF 和图片：调用 OCR API 提取文字和表格；
- Excel：读取工作表、表头及费用明细行；
- Word：提取正文和表格；
- EML 附件应根据实际格式递归解析。

### 2. 航运业务信息提取

系统需要识别：

- 单据类型；
- 单据编号及日期；
- 签发方和接收方；
- 费用名称；
- 数量、单价和金额；
- 币种；
- PP（预付）、CC（到付）等付款方式；
- 小计、税额和总计。

### 3. 费用名称标准化

将不同写法映射为统一的航运费用名称，例如：

| 原始名称 | 标准名称 |
| --- | --- |
| Handling Fee、H/D Fee | Handling Charge |
| Doc Fee | Documentation Fee |
| THC | Terminal Handling Charge |
| Trucking Fee | Trucking Charge |
| Customs Fee | Customs Clearance Fee |
| Storage Fee | Storage Charge |

系统应同时保存原始费用名称和标准化后的费用名称。

### 4. 新增账单

当输入文件包含有效费用，且没有匹配到已有账单时，将操作类型标记为：

```json
"operation": "CREATE"
```

一个单据中存在多个费用项目时，每个费用项目生成一条记录。

### 5. 更新已有账单

系统按照以下信息匹配已有账单：

1. 单据编号和费用名称；
2. 提单号、费用名称和币种；
3. 订舱号、费用名称和币种；
4. 箱号、交易双方、日期和金额。

匹配成功后，只修改输入材料明确要求变更的字段：

```json
"operation": "UPDATE"
```

没有被明确修改的字段应继承原账单值。

### 6. 应收应付判断

系统应从当前记账主体的视角判断：

- `AR`：应收账款；
- `AP`：应付账款；

不能仅根据 Debit Note 或 Credit Note 直接判断 AR/AP，还必须结合签发方、接收方、当前记账主体和收费方向。

### 7. 数据校验

系统需要校验：

- 金额是否为有效数字；
- 币种是否符合 ISO 4217；
- 明细金额与合计金额是否一致；
- OCR 是否存在 `0/O`、`1/I` 等识别错误；
- 同一费用是否被重复提取。

## 五、标准输出格式

```json
[
  {
    "operation": "UPDATE",
    "document_info": {
      "document_type": "Debit Note",
      "reference_no": "DN-202609-001",
      "document_date": "2026-09-20"
    },
    "transaction_party": {
      "issuer": "Destination Agent",
      "recipient": "Origin Agent"
    },
    "charge_details": {
      "charge_item": "Handling Charge",
      "amount": 40.00,
      "currency": "USD",
      "payment_term": "PP"
    },
    "financial_attributes": {
      "accounting_entity": "Origin Agent",
      "posting_type": "AP"
    },
    "audit_trail": {
      "ai_reasoning": [
        "通过单据编号和费用名称匹配到已有账单",
        "根据 OCR 识别结果将金额更新为 USD 40.00",
        "其他字段未被明确要求修改，因此保留原值"
      ]
    }
  }
]
```

## 六、主要字段说明

- `operation`：账单操作类型，包括 `CREATE`、`UPDATE`、`NO_CHANGE` 和 `PENDING_REVIEW`；
- `document_info`：单据类型、单据编号及单据日期；
- `transaction_party`：单据签发方和接收方；
- `charge_details`：费用名称、金额、币种及付款方式；
- `financial_attributes`：当前记账主体及应收应付属性（AP/AR）；
- `audit_trail`：处理依据。

## 七、处理要求

1. 不得虚构输入材料中不存在的数据；
2. 无法提取的字段使用 `null`；
3. 金额必须使用数字类型；
4. 币种统一使用三位代码，如 `USD`、`CNY`；
5. 每个费用项目单独输出一条记录；
6. 更新账单时不得覆盖未明确要求修改的字段；
8. OCR 结果不能未经校验直接入账；
10. 最终结果必须是合法 JSON。

> 注：原文编号即为 1、2、3、4、5、6、8、10，缺失 7 与 9，按原样保留。

## 八、验收标准

**请构建一个 AI AGENT 它可以完成如下工作：**

1. 能够正确解析 EML 邮件正文及附件；
2. 能够调用 OCR API 识别 PDF 和图片；
3. 能够解析 Excel、Word 中的费用表格；
4. 能够准确提取单据编号、费用、金额和币种；
5. 能够区分新增账单和更新账单；
6. 能够结合记账主体判断 AR/AP；
7. 能够检测重复账单和数据冲突；
8. 当前场景下如何设计 harness 工程提升稳定性和输出准确性；
9. 写出 5 点以上自己的思考，对上述要求进行补充，业务上，技术上都可；
10. 需要基于 python 和 langchain 代码实现。


---

## 附录 A · 题面要求 → 本仓库实现

| 题面条目 | 实现位置 | 验证方式 |
| --- | --- | --- |
| 二.1/2 EML 正文、邮件头、附件递归 | [`src/sailbill/ingest/eml.py`](src/sailbill/ingest/eml.py)、[`router.load_tree`](src/sailbill/ingest/router.py) | [`tests/test_ingest.py`](tests/test_ingest.py) |
| 二.3 OCR API | [`src/sailbill/ingest/ocr.py`](src/sailbill/ingest/ocr.py) | [`tests/test_ocr_adapter.py`](tests/test_ocr_adapter.py)、`sailbill ocr <file>` |
| 四.1 Excel/Word 费用表 | [`ingest/excel.py`](src/sailbill/ingest/excel.py)、[`ingest/word.py`](src/sailbill/ingest/word.py)、[`extract/tables.py`](src/sailbill/extract/tables.py) | `tests/test_ingest.py` |
| 四.2 业务要素提取 | [`src/sailbill/extract/rules.py`](src/sailbill/extract/rules.py) + [`lexicon.py`](src/sailbill/extract/lexicon.py) | `harness/run_eval.py` |
| 四.3 费用名称标准化（含题面 6 组映射） | [`src/sailbill/normalize/charges.py`](src/sailbill/normalize/charges.py) | `tests/test_normalize.py::test_required_charge_mappings` |
| 四.4/5 CREATE / UPDATE + 四级匹配 | [`pipeline.py`](src/sailbill/pipeline.py)、[`matching.py`](src/sailbill/matching.py) | `tests/test_matching.py`、`tests/test_pipeline.py` |
| 四.6 AR/AP | [`src/sailbill/finance.py`](src/sailbill/finance.py) | `tests/test_finance_validate.py` |
| 四.7 数据校验（含 OCR `0/O`、`1/I`） | [`validate/business.py`](src/sailbill/validate/business.py)、[`normalize/amounts.py`](src/sailbill/normalize/amounts.py) | `tests/test_amounts.py`、`tests/test_finance_validate.py` |
| 五/六 输出契约 | [`src/sailbill/models.py`](src/sailbill/models.py)、[`output.py`](src/sailbill/output.py) | `tests/test_pipeline.py::test_output_schema_is_exactly_the_contract` |
| 八.8 harness 工程 | [`harness/run_eval.py`](harness/run_eval.py)、[`docs/harness.md`](docs/harness.md) | `sailbill eval --min-score 0.95` |
| 八.9 5 点以上思考 | [`docs/reflections.md`](docs/reflections.md)（12 点） | 文档 |
| 二 语言框架 python + langchain | [`extract/llm.py`](src/sailbill/extract/llm.py)（langchain-core + langchain-openai） | `docs/architecture.md` |

设计取舍见 [docs/architecture.md](docs/architecture.md) 与 `docs/adr/0001-0004`。

## 附录 B · 仓库约定（给后续维护者 / AGENT）

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,llm]"
python scripts/make_samples.py                 # 生成合成样本
pytest -q                                      # 148 个测试，全离线
python harness/run_eval.py --min-score 0.95    # 金标评测 + CI 门禁
python -m sailbill demo                        # 端到端演示

# 真实样本（TestSamples/ 需自行放入，不入库）
SAILBILL_ACCOUNTING_ENTITY="你的公司全称" python -m sailbill run TestSamples --store out/no-store.json
pytest tests/test_real_samples.py              # 真实样本回归（无样本时自动跳过）
```

**两个必须显式配置、否则结果会"看起来没跑通"的项**

- `SAILBILL_ACCOUNTING_ENTITY`（或 `--entity`）：没有记账主体就无法判定 AR/AP，记录会转
  `PENDING_REVIEW`——这是刻意设计而非 bug；真实 Debit Note 配上主体即输出 `CREATE / AP`。
- OCR 的**公网投递**：OCR 服务只会下载 URL，访问不到 `localhost`。本地文件需通过
  `SAILBILL_FILE_DELIVERY`（`public` / `upload`）变成可下载地址，或设 `none` 走离线回放；
  未配置时图片类文件转 `PENDING_REVIEW`。

**红线（任何改动都不得违反）**

1. 不得提交真实客户数据、真实运费、公司名或密钥；真实样本目录 `TestSamples/`、`samples/private/` 已在 `.gitignore`。
2. 不得为了"看起来更好"而虚构字段值：原文找不到依据就置 `null` 并转 `PENDING_REVIEW`（题面七.1）。
3. 不得在判不出 AR/AP 时默认给 `AP`；不得用 `null` 覆盖已有账单中原值（题面四.6、四.5）。
4. 默认输出必须严格符合题面第五节契约；调试信息只能进 `--extended` 的 `_trace`。
5. 改动 `extract/` 或 `normalize/` 后必须重跑评测，综合分不得下降。

**代码结构**：`ingest`（解析）→ `extract`（抽取/仲裁/锚定）→ `normalize`（标准化）→
`validate`（校验/置信度）→ `matching` + `finance`（决策）→ `output`/`pipeline`（契约与编排）。
