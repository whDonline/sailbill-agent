# 多格式解析：用了什么，哪些换成了成熟开源，哪些坚持自研

对应题面 **四.1 多格式文档解析**。这张表是"每个格式到底怎么解锁的"的完整交代，
包括**实测后被否掉**的方案——`pdfplumber` 就是一例。

## 一、总表

| 输入 | 现行方案 | 许可证 | 性质 | 是否可换成熟开源 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **EML** 头/正文/附件 | Python 标准库 `email`（`policy.default`） | PSF | 成熟 | 无需更换 | 标准库即最佳实践，`mailparse`/`extract-msg` 只用于 .msg |
| EML 的 HTML 正文 | **`beautifulsoup4`**（刚替换，旧的手写解析器留作 fallback） | MIT | 成熟 | ✅ 已换 | 邮件正文多嵌套表格，bs4 的 `decompose()`+表转 `a | b` 更稳 |
| **MSG**（Outlook） | **`extract-msg`**（可选依赖 `.[msg]`） | MIT | 成熟 | ✅ 已换 | `.msg` 是 OLE 容器，**不能**交给 `email` 模块；原实现会直接失败 |
| **PDF 文本层** | `pypdf` | BSD-3 | 成熟 | 可换 `pdfminer.six`/`PyMuPDF` | 保留：够用、无 AGPL 风险（PyMuPDF 是 AGPL） |
| **PDF 表格** | `pdfplumber`（MIT）**仅当页面有 ≥8 条框线**；无框线走自写行解析 | MIT | 成熟 | ✅ 部分采用 | 见下方实测：无框线账单上 pdfplumber 会把单元格切碎 |
| **扫描件/图片** | 题面指定的 OCR 服务 + 自写容错适配器 | — | 半自研 | 可加 PaddleOCR / Surya 作本地兜底 | 保留：OCR 的难点是"服务只认 URL"的契约，不是识别本身 |
| **Excel (.xlsx)** | `openpyxl` | MIT | 成熟 | 无需更换 | 保留：能拿到行号/原始单元格，比 pandas 更适合版式还原 |
| **Excel (.xls)** | `xlrd`（可选依赖 `.[xls]`） | BSD | 成熟 | ✅ 已接入 | 旧 BIFF 格式，openpyxl 不支持 |
| **CSV** | 标准库 `csv` | PSF | 成熟 | 无需更换 | 多编码探测（utf-8-sig/gb18030/latin-1） |
| **Word (.docx)** | `python-docx` | MIT | 成熟 | 可换 `docx2python`（嵌套表更强） | 保留：已按 `_tc` 身份正确处理合并单元格 |
| **Word (.doc)** | 提示用 LibreOffice 转换 | — | 未实现 | 可用 `soffice --convert-to docx` | 明确提示，不静默失败 |
| **附件递归** | 自写 `load_tree`（深度上限 + `sha256` 去重 + `parent_id`） | — | **自研** | `unstructured` 可做，但会压平来源信息 | 保留：这里承载"邮件是容器、附件才是账单本体"的业务语义 |
| **文本行费用表** | 自写（`rate qty P/C amount`、`描述 … USD 245.0000` 等形态） | — | **自研** | 开源库均无此能力 | 保留：这是领域资产，也是真实样本喂出来的 |

## 二、实测：pdfplumber 不是万能的

同一份真实 Debit Note（`TestSamples/PDF 示例账单2.pdf`），三种表策略的实际输出：

| 策略 | 结果 |
| --- | --- |
| 默认（lines） | 找到表，但**三行数据被合并成一行**：`['-\\n-\\n-', '65.00\\n1,310.40\\n35.00', ...]` |
| `vertical_strategy="text"` | 更糟：单元格被**从词中间切开** —— `'DU'`/`'E DATE'`、`'TE'`/`'RMS'`、`'1,'`/`'6/31/3'` |
| 自写行解析 | ✅ `HANDLING CHARGE 65.00` / `ADD'L DETENTION 1,310.40` / `HANDLING CHARGE 35.00`，合计 **1410.40 = 单据总额** |

原因很朴素：**货代账单大多没有框线**，是靠空格对齐的视觉表格。几何引擎（pdfplumber）
擅长"有框线"的表；无框线的表要靠"行文本 + 列语义"，这恰好是自写解析器做的事。

所以最终设计是**按页分流**：

```
页面有 ≥8 条 lines/rects  →  pdfplumber 提取表格（并炸开多行合并单元格）
否则                      →  自写行解析（rate qty P/C amount / 描述 … 币种 金额）
两者都抽到时              →  用文本行补齐（仅当合计不闭合，且只补新行，避免重复计数）
```

## 三、被否掉的方案及原因

| 方案 | 为什么没用 |
| --- | --- |
| **Docling**（MIT，表格能力强） | 依赖 torch + 模型权重（数百 MB），CI 与本沙箱都跑不动；对"有文本层的账单 PDF"没有增益。已列为可选后端 |
| **MinerU / Marker / Surya 做主力** | 同上，且 MinerU 是 AGPL、Marker 有商业限制，与 MIT 开源冲突 |
| **PyMuPDF (fitz)** | AGPL-3.0，SaaS 场景有传染风险 |
| **`unstructured` 统一入口** | 会把"哪个附件、第几页、第几行"压平成文本块，丢失我们做证据锚定需要的 provenance |
| **`invoice2data`** | 它对**固定版式**很有效（YAML 模板 + 正则），适合作为"大客户专用模板"接入；但模板要人手写，覆盖不了长尾版式。已列入路线图 |
| **pandas 读 Excel** | 丢失合并单元格与行号，反而更难还原版式 |

## 四、许可证边界（重要）

| 类别 | 项目 | 能否直接依赖 |
| --- | --- | --- |
| MIT / BSD / Apache | bs4、pdfplumber、pypdf、openpyxl、python-docx、extract-msg、xlrd、LangGraph、instructor | ✅ 可以 |
| GPL / AGPL | paperless-ngx、ERPNext、MinerU、Marker、PyMuPDF | ⚠️ 只能作为**独立服务**通过 API 调用，不能 import 进本仓库 |

本项目全部依赖均为 MIT/BSD/Apache/PSF，可直接商用与开源。

## 五、一句话总结

> 成熟库负责**"把文件变成文本和表格"**（bs4 / pypdf / pdfplumber / openpyxl / python-docx / extract-msg），
> 自研代码只负责**"把文本和表格变成可入账的账务事实"**（费用行形态、列语义、三态字段、证据锚定）。
> 前者换得越彻底越好，后者一行都不该外包。
