# Harness 工程：把"看起来能跑"变成"可度量、可回归、可解释"

题目第 8 条要求说明**如何通过 harness 工程提升稳定性和输出准确性**。本文是这一条的实现说明，
每一条都对应仓库里可执行的代码，而不是理念。

## 0. 一句话

> 让模型只在**它比代码强**的地方说话，让所有**能被验证**的东西都被验证，
> 再把验证结果变成**可回放的测试与可门禁的指标**。

---

## 1. 十二个机制

### 1.1 确定性优先（Deterministic-first）

金额、合计、币种、费用名、匹配、AR/AP 全部由代码完成（见 `docs/architecture.md` §3）。
模型只处理版式语义与模糊归属。**收益**：即使模型输出错误，也不会产生非法 JSON 或算错的合计。

### 1.2 契约校验 + 自修复环

`extract/llm.py`：

```python
for attempt in range(1, max_attempts + 1):     # 默认 2 次
    payload = self._invoke(user_text, hints)
    doc = self._to_extraction(payload)         # Pydantic 校验 + 归一化
    return LLMResult(doc=doc, ...)
# 失败时把 errors 追加进 prompt 重试；仍失败则整个 LLM 环被 extract_document 捕获并回退规则模式
```

输出侧再用 `BillRecordOut.model_validate()` 二次校验（`harness/run_eval.py` 的 `schema_valid` 指标）。

### 1.3 证据锚定（Grounding）—— 最划算的反幻觉手段

`extract/grounding.py`：每个抽出的字符串要在原文（去标点、小写归一后）出现，每个金额要能在原文
找到 `3220.00` / `3,220.00` / `3220` 之一；否则**丢弃或降级为 `unknown`**，并写入 warning。

比在 prompt 里写十遍"不要虚构"有效得多，而且能被评审用一行代码验证：

```python
assert ext.total_amount.status == Status.UNKNOWN   # 9999.0 不在原文里
```

### 1.4 双通道一致性（Dual read）

同一份文档的同一个数字有两条独立来源：

* PDF 的**文本层**（`pypdf`）与 **OCR 结构化结果**（`SAILBILL_OCR_FOR_PDF=true` 时同时取）；
* 表格解析出的金额与"数量 × 单价"推导出的金额（`extract/tables.py`）；
* 规则引擎结果与 LLM 结果（`extract/__init__.merge()`）。

不一致不会被静默吞掉，而是变成 `[CONFLICT]` warning → 降低置信度 → 可能转 `PENDING_REVIEW`。

### 1.5 冲突仲裁策略（不对称合并）

`merge()` 的取舍是刻意不对称的：

| 字段类型 | 谁赢 | 理由 |
| --- | --- | --- |
| 费用行、金额 | **规则引擎** | 表格单元格是"转录"，模型是"解读" |
| 规则缺失的文档级字段 | 模型补充 | 模型擅长理解版式与语言 |
| 双方都有且不一致 | 规则值 | 且记 `[CONFLICT]`，降置信度 |

### 1.6 可解释置信度

`validate/confidence.py` 从 1.0 起逐项扣分，每项都带人类可读原因，
并**直接写进 `audit_trail.ai_reasoning`**：

```
置信度 0.98，扣分项：-0.02 匹配得分 0.90
```

阈值 `SAILBILL_CONFIDENCE_REVIEW`（默认 0.60）以下 → `PENDING_REVIEW`。
分数本身不重要，**能解释为什么送审**才重要。

### 1.7 金标集与九项指标

`harness/run_eval.py` + `eval/golden/*.json`（6 个用例，覆盖 PDF/Excel/Word/EML/AR场景/脏数据）：

| 指标 | 定义 | 用途 |
| --- | --- | --- |
| `doc_fields` | 单据 5 个字段的准确率 | 防整体退化 |
| `charge_precision / recall / F1` | 费用行按（标准名, 金额, 币种）匹配 | 核心能力 |
| `amount_accuracy` | 金额精确到分 | 财务红线 |
| `currency_accuracy` | 币种正确 | 财务红线 |
| `operation_accuracy` | CREATE/UPDATE/NO_CHANGE/PENDING 判定 | 业务正确性 |
| `posting_accuracy` | AR/AP 判定 | 业务正确性 |
| `schema_valid` | 输出是否符合契约 | 接口稳定性 |
| `hallucinated_fields` | 期望为空却填了值的字段数 | **反幻觉**（越低越好）|
| `score` | 加权综合分 | CI 门禁 |

```bash
$ python harness/run_eval.py --min-score 0.9
## 金标评测结果
- 用例数：6，存在差异的用例：0
- 综合得分：1.000
| 单据字段准确率 | 1.000 |
| 费用提取 F1     | 1.000 |
| 金额精确一致率  | 1.000 |
| 操作类型准确率  | 1.000 |
| AR/AP 准确率    | 1.000 |
| 虚构字段数      | 0     |
```

`--min-score` 未达标返回非零退出码，CI 里直接当门禁用。

### 1.8 录制回放（Cassette）

* OCR 响应按 `sha256` 缓存在 `.cache/sailbill/ocr/`，重跑零成本、结果确定；
* 文件 → URL 的映射缓存在 `.cache/sailbill/urls.json`，链接失效时自动失效重传一次；
* 测试全部用合成样本 + 离线模式，`pytest` 不发任何网络请求。

### 1.9 幂等（Idempotency）

* 同一文件重复投递：`sha256` 相同，`BillStore` 按业务键 `upsert`，不会开两张账单；
* 同一批文件重复跑：输出逐字节一致（`test_runs_are_deterministic`）。

### 1.10 成本与延迟预算

| 手段 | 效果 |
| --- | --- |
| 文本层优先，扫描件才 OCR | 大多数电子账单 0 次 OCR 调用 |
| 两段式抽取（文档级 + 费用行） | 上下文短、单文档 1–3 次 LLM 调用 |
| 温度 0 + 结构化输出 | 避免重试 |
| OCR 30 页上限、文本 12 万字符上限 | 防超长文档拖垮超时 |
| `timeout=(10, 300)` 与服务端 300s 对齐 | 不假死 |

### 1.11 可观测性（Trace）

`sailbill run --trace out/trace.json` 每个文档一条：文件、sha256、类型、上下文文档、
抽取器（rules/llm/hybrid）、费用条数、校验问题数、操作分布、记账主体、OCR 是否命中缓存、耗时。

出问题时先看 trace，再看 `--extended` 里单条记录的证据与扣分项，最后才去读模型调用。

### 1.12 降级路径（Graceful degradation）

任何外部依赖（LLM / OCR / 文件投递）失败都不抛出到顶层，而是记录原因并产出
`PENDING_REVIEW` —— **宁可交给人，不可猜给账**。这是财务场景的基本职业道德。

---

## 2. 如何新增一个金标用例

1. 把（脱敏后的）文件放到 `samples/`；
2. 新建 `eval/golden/NN_xxx.json`：

```json
{
  "name": "my_case",
  "files": ["samples/my_bill.pdf"],
  "store": "samples/existing_bills.json",
  "accounting_entity": null,
  "expect": {
    "document": {"document_type": "Debit Note", "reference_no": "DN-1", "document_date": "2026-09-20",
                 "issuer": "...", "recipient": "..."},
    "charges": [
      {"charge_item": "Ocean Freight", "amount": 100.0, "currency": "USD",
       "payment_term": "PP", "operation": "CREATE", "posting_type": "AP"}
    ]
  }
}
```

3. `python harness/run_eval.py --min-score 0.9`，指标下降就说明改坏了。

## 3. 当前局限（诚实说明）

* 金标集只有 6 个合成用例，覆盖面依靠单测补足（136 个），真实版式多样性不足；
* 第 4 级匹配（箱号+交易方+日期+金额）依赖日期窗口 120 天，属于经验值；
* 多币种单据只做"识别"，不做折算（缺汇率字段，见 `docs/reflections.md`）；
* OCR 服务返回的是领域结构化数据，若它对某类版式返回空 `chargeList`，本系统只能转人工复核 ——
  这正是 `PENDING_REVIEW` 存在的意义，而不是硬猜。
