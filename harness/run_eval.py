"""Golden-set evaluation harness.

This is the part the assignment calls "harness engineering": a reproducible way
to answer *"did my change make the agent better or worse?"* without eyeballing
outputs.

It measures what actually matters for a finance agent:

======================  ====================================================
schema validity         every record matches the documented contract
document fields         type / reference / date / issuer / recipient
charge extraction       precision, recall, F1 over (name, amount, currency)
amount exactness        amounts equal to the cent, not "close"
operation accuracy      CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW
AR/AP accuracy          the posting type per charge
hallucination rate      fields filled that the document does not contain
======================  ====================================================

Usage::

    python harness/run_eval.py                 # deterministic (rules only)
    python harness/run_eval.py --llm           # include the model
    python harness/run_eval.py --min-score 0.9 # CI gate, non-zero exit on drop
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sailbill.config import SETTINGS  # noqa: E402
from sailbill.models import BillRecordOut  # noqa: E402
from sailbill.normalize.charges import standardize_charge  # noqa: E402
from sailbill.pipeline import run_paths  # noqa: E402
from sailbill.store import BillStore  # noqa: E402

GOLDEN_DIR = ROOT / "eval" / "golden"
DOC_FIELDS = ("document_type", "reference_no", "document_date", "issuer", "recipient")

WEIGHTS = {
    "doc_fields": 0.20,
    "charge_f1": 0.25,
    "amount_accuracy": 0.15,
    "operation_accuracy": 0.20,
    "posting_accuracy": 0.10,
    "currency_accuracy": 0.05,
    "schema_valid": 0.05,
}


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    name: str
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    doc_hits: int = 0
    doc_total: int = 0
    charge_tp: int = 0
    charge_fp: int = 0
    charge_fn: int = 0
    amount_hits: int = 0
    currency_hits: int = 0
    currency_total: int = 0
    operation_hits: int = 0
    operation_total: int = 0
    posting_hits: int = 0
    posting_total: int = 0
    hallucinated: int = 0
    hallucination_slots: int = 0
    schema_ok: bool = True
    produced: int = 0
    expected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "errors": self.errors,
            "doc_fields": f"{self.doc_hits}/{self.doc_total}",
            "charge": {"tp": self.charge_tp, "fp": self.charge_fp, "fn": self.charge_fn},
            "operation": f"{self.operation_hits}/{self.operation_total}",
            "posting": f"{self.posting_hits}/{self.posting_total}",
            "hallucinated": self.hallucinated,
            "schema_ok": self.schema_ok,
        }


# --------------------------------------------------------------------------- #
# comparison helpers
# --------------------------------------------------------------------------- #
def _norm(text: Any) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split()).strip().lower()


def _charge_key(name: Any) -> str:
    return _norm(standardize_charge(name).standard or name)


def _match_charges(expected: list[dict], produced: list[dict], tol: float = 0.01):
    """Greedy bipartite match on standardised charge name."""
    remaining = list(range(len(produced)))
    pairs: list[tuple[int, int]] = []
    for i, exp in enumerate(expected):
        key = _charge_key(exp.get("charge_item"))
        for j in remaining:
            if _charge_key(produced[j].get("charge_item")) == key:
                pairs.append((i, j))
                remaining.remove(j)
                break
    unmatched_expected = [i for i in range(len(expected)) if i not in {p[0] for p in pairs}]
    return pairs, unmatched_expected, remaining


def evaluate_case(case_path: Path, *, use_llm: bool, offline: bool) -> CaseResult:
    spec = json.loads(case_path.read_text(encoding="utf-8"))
    result = CaseResult(name=spec.get("name") or case_path.stem)

    store = BillStore.from_file(ROOT / spec["store"]) if spec.get("store") else BillStore()
    files = [ROOT / item for item in spec["files"]]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        result.ok = False
        result.errors.append(
            f"缺少输入文件: {missing}（TestSamples/ 为真实样本，不随仓库分发，仅本地评测）"
        )
        return result

    from dataclasses import replace

    settings = replace(SETTINGS, offline=offline)
    run = run_paths(
        files,
        settings=settings,
        store=store,
        accounting_entity=spec.get("accounting_entity"),
        use_llm=use_llm,
    )

    records = [record.model_dump(mode="json") for record in run.records]
    expected = spec["expect"]
    exp_doc = expected.get("document", {})
    exp_charges: list[dict] = expected.get("charges", [])

    # ---- schema ---------------------------------------------------------
    for payload in records:
        try:
            BillRecordOut.model_validate(payload)
        except Exception as exc:  # pragma: no cover
            result.schema_ok = False
            result.errors.append(f"schema 校验失败: {exc}")
            break

    # ---- document level --------------------------------------------------
    if records:
        first = records[0]
        got_doc = {
            "document_type": first["document_info"]["document_type"],
            "reference_no": first["document_info"]["reference_no"],
            "document_date": first["document_info"]["document_date"],
            "issuer": first["transaction_party"]["issuer"],
            "recipient": first["transaction_party"]["recipient"],
        }
    else:
        got_doc = {}

    for field_name in DOC_FIELDS:
        # 用例可用 doc_ignore 跳过无业务意义的字段（如邮件头里的发件人）
        if field_name in set(spec.get("doc_ignore") or ()):
            continue
        result.doc_total += 1
        want = exp_doc.get(field_name)
        got = got_doc.get(field_name)
        if want is None and got is None:
            result.doc_hits += 1
        elif _norm(want) == _norm(got):
            result.doc_hits += 1
        elif field_name == "document_date" and _norm(want) == _norm(got):
            result.doc_hits += 1
        if want is None and got not in (None, ""):
            result.hallucinated += 1
        result.hallucination_slots += 1

    # ---- charges ----------------------------------------------------------
    result.expected = len(exp_charges)
    result.produced = len(records)
    # charge_item 为 None 的记录是"无费用转人工复核"的占位记录，不构成费用提取
    produced_charges = [
        record["charge_details"]
        for record in records
        if record["charge_details"].get("charge_item") is not None
    ]
    pairs, missing_expected, extra = _match_charges(exp_charges, produced_charges)
    result.charge_tp = len(pairs)
    result.charge_fp = len(extra)
    result.charge_fn = len(missing_expected)

    for exp_index, got_index in pairs:
        exp, got = exp_charges[exp_index], records[got_index]
        charge = got["charge_details"]

        if exp.get("amount") is not None:
            if abs(float(charge.get("amount") or 0) - float(exp["amount"])) <= 0.01:
                result.amount_hits += 1
        if exp.get("operation"):
            result.operation_total += 1
            if got["operation"] == exp["operation"]:
                result.operation_hits += 1
        if exp.get("posting_type"):
            result.posting_total += 1
            if got["financial_attributes"].get("posting_type") == exp["posting_type"]:
                result.posting_hits += 1
        if exp.get("currency"):
            result.currency_total += 1
            if _norm(charge.get("currency")) == _norm(exp["currency"]):
                result.currency_hits += 1
        if exp.get("payment_term"):
            if _norm(charge.get("payment_term")) != _norm(exp["payment_term"]):
                result.errors.append(
                    f"付款方式不符: {charge.get('charge_item')} "
                    f"期望 {exp['payment_term']} 实际 {charge.get('payment_term')}"
                )

    if missing_expected:
        names = [exp_charges[i].get("charge_item") for i in missing_expected]
        result.errors.append(f"漏提费用: {names}")
    if extra:
        names = [records[j]["charge_details"]["charge_item"] for j in extra]
        result.errors.append(f"多提费用: {names}")

    return result


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    def ratio(num: int, den: int) -> float:
        return round(num / den, 4) if den else 1.0

    tp = sum(r.charge_tp for r in results)
    fp = sum(r.charge_fp for r in results)
    fn = sum(r.charge_fn for r in results)
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0

    metrics = {
        "doc_fields": ratio(sum(r.doc_hits for r in results), sum(r.doc_total for r in results)),
        "charge_f1": f1,
        "charge_precision": precision,
        "charge_recall": recall,
        "amount_accuracy": ratio(sum(r.amount_hits for r in results), tp or 1),
        "currency_accuracy": ratio(sum(r.currency_hits for r in results), sum(r.currency_total for r in results)),
        "operation_accuracy": ratio(sum(r.operation_hits for r in results), sum(r.operation_total for r in results)),
        "posting_accuracy": ratio(sum(r.posting_hits for r in results), sum(r.posting_total for r in results)),
        "schema_valid": 1.0 if all(r.schema_ok for r in results) else 0.0,
    }
    score = sum(metrics[key] * weight for key, weight in WEIGHTS.items())
    metrics["score"] = round(score, 4)
    metrics["hallucinated_fields"] = sum(r.hallucinated for r in results)
    metrics["cases"] = len(results)
    metrics["cases_with_errors"] = sum(1 for r in results if r.errors)
    return metrics


def report(results: list[CaseResult], metrics: dict[str, Any]) -> str:
    lines = [
        "## 金标评测结果",
        "",
        f"- 用例数：**{metrics['cases']}**，存在差异的用例：**{metrics['cases_with_errors']}**",
        f"- 综合得分：**{metrics['score']:.3f}**",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
    ]
    labels = {
        "doc_fields": "单据字段准确率",
        "charge_precision": "费用提取精确率",
        "charge_recall": "费用提取召回率",
        "charge_f1": "费用提取 F1",
        "amount_accuracy": "金额精确一致率",
        "currency_accuracy": "币种准确率",
        "operation_accuracy": "操作类型准确率",
        "posting_accuracy": "AR/AP 准确率",
        "schema_valid": "输出结构合规率",
        "hallucinated_fields": "虚构字段数（越低越好）",
    }
    for key, label in labels.items():
        value = metrics[key]
        shown = f"{value:.3f}" if isinstance(value, float) else str(value)
        lines.append(f"| {label} | {shown} |")

    lines += ["", "### 逐用例", "", "| 用例 | 单据字段 | 费用 TP/FP/FN | 操作 | AR/AP | 结构 | 备注 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in results:
        charge = item.as_dict()["charge"]
        note = "；".join(item.errors[:2]) if item.errors else "OK"
        lines.append(
            f"| {item.name} | {item.doc_hits}/{item.doc_total} | "
            f"{charge['tp']}/{charge['fp']}/{charge['fn']} | "
            f"{item.operation_hits}/{item.operation_total} | "
            f"{item.posting_hits}/{item.posting_total} | "
            f"{'✅' if item.schema_ok else '❌'} | {note} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(dataset: str | Path | None = None, *, verbose: bool = False, use_llm: bool = False,
         min_score: float | None = None, out: str | Path | None = None) -> int:
    dataset_dir = Path(dataset) if dataset else GOLDEN_DIR
    if not dataset_dir.is_absolute():
        dataset_dir = ROOT / dataset_dir
    cases = sorted(dataset_dir.glob("*.json"))
    if not cases:
        print(f"没有找到金标用例: {dataset_dir}", file=sys.stderr)
        return 2

    results = [evaluate_case(path, use_llm=use_llm, offline=not use_llm) for path in cases]
    metrics = aggregate(results)
    text = report(results, metrics)
    print(text)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    print("\n```json\n" + json.dumps(metrics, ensure_ascii=False, indent=2) + "\n```")

    if verbose:
        for item in results:
            if item.errors:
                print(f"[{item.name}]", file=sys.stderr)
                for error in item.errors:
                    print(f"  - {error}", file=sys.stderr)

    if min_score is not None and metrics["score"] < min_score:
        print(f"评测未达标：{metrics['score']:.3f} < {min_score:.3f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sailbill golden-set evaluation")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--llm", action="store_true", help="包含大模型抽取（需要 API Key）")
    parser.add_argument("--min-score", type=float, default=None, help="低于该分数返回非零（CI 门禁）")
    parser.add_argument("--out", default=None, help="评测报告输出路径")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        main(args.dataset, verbose=args.verbose, use_llm=args.llm, min_score=args.min_score, out=args.out)
    )
