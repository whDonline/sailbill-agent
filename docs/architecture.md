# 架构设计

## 1. 分层与数据流

```
文件 ──> ingest ──> NormalizedDocument ──> extract ──> DocExtraction
                                              │
                                    validate / grounding
                                              │
                              matching ──> finance ──> Decision ──> BillRecordOut
```

每一层的输出都是可序列化的 Pydantic 模型，所以任何一层都能单独测试、单独 dump、单独回放。

| 层 | 输入 | 输出 | 关键不变量 |
| --- | --- | --- | --- |
| ingest | 文件路径 | `NormalizedDocument` | 每段文本都带来源（文件/页/行/表格坐标） |
| extract | 文档组 | `DocExtraction` | 每个标量都是三态 `Tri`，每行费用都有 `evidence` |
| validate | `DocExtraction` | `Issue[]` | 阻断级问题必须让记录转 `PENDING_REVIEW` |
| matching | 单行费用 + 账单库 | `MatchOutcome` | 高优先级规则永远压过低优先级 |
| finance | 单据 + 记账主体 | `PostingDecision` | 没有主体就不给 AR/AP |
| output | 决策 + 合并后的字段 | `BillRecordOut` | 只允许题目规定的键 |

## 2. 为什么要"文档组"而不是"文件"

一封邮件 + 一个 PDF 附件在业务上是**一张账单**；但一封邮件挂两个 PDF 就是两张账单。
所以 `ingest.load_tree()` 把附件树**拍平成列表**（保留 `parent_id` / `root_id`），
`pipeline.split_units()` 再按"叶子文档"切分成账单单元，父级（邮件正文）只作为**上下文**：

* 邮件正文提供 `From/To`、主题里的单据号、以及"请更新某某费用"这类说明；
* 账单本体永远是附件；
* 邮件正文里的散文**不会**被当费用行 —— 这是 `extract/rules.py` 里显式区分 `primary` 与 `context` 的原因。

## 3. 确定性优先（deterministic-first）

LLM 只做三件事：理解版式语义、判定模糊归属、生成可解释理由。其余全部用代码：

| 任务 | 为什么不用 LLM | 实现 |
| --- | --- | --- |
| 金额解析 | 小数分隔符、千分位、负数括号、OCR 混淆字符 | `normalize/amounts.py` |
| 币种校验 | ISO 4217 是查表问题，模型会"看着像" | `normalize/currency.py` |
| 费用名标准化 | 词典 + 模糊匹配比模型稳定且可审计 | `normalize/charges.py` |
| 合计校验 | 算术必须精确到分 | `validate/business.py` |
| 账单匹配 | 四级规则的优先级是业务规则，不是语义相似度 | `matching.py` |
| AR/AP | 方向错误会记错账，必须可解释 | `finance.py` |

好处：即使模型抽风，输出依然**结构合法、数学自洽**；模型完全不可用（无 Key/超时/额度耗尽）时，
系统退化为纯规则模式仍可跑通全流程 —— 这也是评审能"零配置验收"的原因。

## 4. 三态字段（本项目最关键的设计）

```python
class Tri(Generic[T]):
    value: T | None
    status: Status  # present | absent | unknown
    evidence: str | None
```

* `present`：单据明确写了 → 输出该值；
* `absent`：单据**确实没有**该字段（例如小计=合计、没有税额行）→ `UPDATE` 时**可以继承**原账单值；
* `unknown`：字段存在但读不可靠（OCR 模糊、被截断）→ 既不能继承也不能写入，必须复核。

把它们压成一个 `None` 是这类系统最常见的事故来源：**OCR 失败会把原账单金额覆盖成 null**。

## 5. 更新语义：patch，不是覆盖

`pipeline._decide_and_build()` 的规则：

1. 先算 `changed_fields`（金额/币种/付款方式/费用名/数量/单价）；
2. 只有 `ext` 中**有值**的字段才参与比较 —— `unknown` 不参与，因此不会把原值改成 null；
3. 输出的是**合并后的完整记录**：没被明确变更的字段从原账单继承；
4. `audit_trail` 里逐条写明"哪个字段从多少变到多少"。

## 6. AR/AP 是矩阵不是 if-else

```
记账主体 == 签发方  -> AR（我方开单收钱）
记账主体 == 接收方  -> AP（对方向我方收钱）
都不是              -> None -> PENDING_REVIEW
同一张 DN，换个主体，结论就翻转
```

`Credit Note` 只改变**方向**（`direction = -1`，冲减同一侧），不改变 posting_type 的判定依据。
`tests/test_finance_validate.py::test_document_type_alone_never_decides` 固化了这个行为。

## 7. 输出契约

默认模式**只输出题目 schema 中的 6 个顶层键**，任何额外字段都会破坏下游系统兼容性。
调试信息（置信度、证据、匹配规则、变更字段、校验问题）通过 `--extended` 放进 `_trace` 命名空间，
并会被 `BillRecordOut.model_validate()` 之外单独处理，不参与契约校验。

## 8. 失败模式与降级

| 故障 | 行为 |
| --- | --- |
| 无 LLM Key / 超时 / 429 | 记录 warning，回退规则模式，继续输出 |
| LLM 返回非法 JSON | 把错误回灌重试（≤2 次），仍失败则回退规则模式 |
| OCR 服务不可达 | 记录 warning，该文档无文本 → `PENDING_REVIEW`，不编造 |
| OCR 需要公网 URL 但没有投递方式 | 明确报错并给出四种解决方案（`OcrDeliveryError`） |
| 单个附件解析失败 | 捕获后记 warning，不影响同批其他文件 |
| Excel 旧版 `.xls` | 提示安装 `xlrd` 或先转换，不静默失败 |
