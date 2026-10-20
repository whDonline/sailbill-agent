# ADR 0004 · 把 OCR 返回当作"开放 schema"

- 状态：已采纳
- 日期：2026-09-27

## 背景

题目给的 OCR 服务并没有文档，实测（`GET /openapi.json` + 真实调用）发现：

* 入参是 `{file: <URL>, task: "orderChange", ...}`，服务端**自己下载**文件；
* 返回是**领域结构化 JSON**：`data.list[].chargeList[] / ctrList[] / mblNumber / shipper / consignee`，
  而不是带坐标的纯文本；
* schema 随单据类型变化，`chargeList` 可能是空数组。

## 决策

不绑定字段名，而是：

1. **同义词映射 + 递归查找**（`ingest/ocr.py: find_charges/find_references/find_parties`）——
   兼容 `chargeName / charge_name / fee / description`、`amount / amt / total` 等多种写法；
2. **原始 payload 完整保留**在 `NormalizedDocument.ocr`，可回放、可复查；
3. **同时渲染成可引用文本**（`render_ocr_text`），供证据锚定与审计引用；
4. `chargeList` 为空时**不编造**，交给 `PENDING_REVIEW`。

## 后果

- ✅ 服务端改字段名不会立刻打断链路；
- ✅ 本地文件 → 公网 URL 的投递问题被显式建模（`UrlResolver` 四种策略）；
- ⚠️ "猜字段"本身有风险，命中不确定时会降置信度；
- ⚠️ 若 OCR 服务未来提供 pure-text/bbox 模式，应改成"结构化优先、文本兜底"的双读（见 harness §1.4）。
