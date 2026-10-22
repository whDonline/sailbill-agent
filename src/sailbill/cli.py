"""Command line interface.

    sailbill run samples/debit_note.pdf --store samples/existing_bills.json
    sailbill run samples/ --out out/records.json --store samples/existing_bills.json
    sailbill ocr samples/debit_note.pdf
    sailbill demo
    sailbill eval --dataset eval/golden
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import SETTINGS, Settings
from .ingest import load_tree
from .ingest.ocr import OcrClient, OcrError, find_charges, render_ocr_text
from .normalize.charges import standardize_charge
from .output import as_extended, as_json, summarize
from .pipeline import run_paths
from .store import BillStore


def _settings_from_args(args: argparse.Namespace) -> Settings:
    changes = {}
    if getattr(args, "offline", False):
        changes["offline"] = True
    if getattr(args, "entity", None):
        pass  # entity is not part of Settings
    return replace(SETTINGS, **changes) if changes else SETTINGS


def _expand(paths: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        p = Path(item)
        if p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in {
                    ".eml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
                    ".xlsx", ".xls", ".csv", ".docx", ".doc", ".txt",
                }:
                    out.append(child)
        else:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    store = BillStore.from_file(args.store)
    files = _expand(args.files)
    if not files:
        print("没有可处理的文件", file=sys.stderr)
        return 2

    result = run_paths(
        files,
        settings=settings,
        store=store,
        accounting_entity=args.entity,
        use_llm=not args.no_llm and settings.llm_enabled,
        hitl=args.hitl,
        commit=args.commit,
    )

    payload = [
        as_extended(record, extra) if args.extended else as_json(record)
        for record, extra in zip(result.records, result.extras, strict=True)
    ]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    if not args.quiet:
        print(text)
    summary = summarize(result.records)
    print(
        f"[sailbill] 文件 {len(files)} 个，记录 {summary['records']} 条，"
        f"分布 {summary['by_operation']}，待复核 {summary['needs_review']} 条",
        file=sys.stderr,
    )
    if (
        summary["needs_review"]
        and not args.entity
        and not settings.accounting_entity
        and not store.common_entity()
    ):
        print(
            "[sailbill] 提示：未指定记账主体，AR/AP 无法判定，相关记录已转 PENDING_REVIEW。\n"
            '          用 --entity "<你的公司全称>" 或设置 SAILBILL_ACCOUNTING_ENTITY 后可入账。',
            file=sys.stderr,
        )

    if args.trace:
        trace_path = Path(args.trace)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(result.traces, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.save_bills:
        store.save(args.save_bills)
        print(f"[sailbill] 账单库已保存到 {args.save_bills}（{len(store)} 张）", file=sys.stderr)

    paused = [t for t in result.traces if t.get("paused_before_commit")]
    if paused:
        print(
            f"[sailbill] {len(paused)} 个单据已在写账前暂停（LangGraph interrupt），"
            "确认后可 resume 或重新运行并去掉 --hitl",
            file=sys.stderr,
        )
    return 0


def cmd_names(args: argparse.Namespace) -> int:
    """题面四.3：原始名称与标准化名称同时保留。这里把两者并排打印出来。"""
    from .extract import extract_document

    files = _expand(args.files)
    if not files:
        print("没有可处理的文件", file=sys.stderr)
        return 2

    settings = _settings_from_args(args)
    rows: list[tuple[str, str, str, str, float, str]] = []
    for path in files:
        docs = load_tree(path, settings=settings)
        extraction = extract_document(docs, settings=settings, use_llm=False)
        for charge in extraction.charges:
            rows.append(
                (
                    path.name,
                    charge.charge_item_raw or "",
                    charge.charge_item or "",
                    standardize_charge(charge.charge_item_raw or "").method,
                    standardize_charge(charge.charge_item_raw or "").score,
                    charge.amount if charge.amount is not None else 0.0,
                )
            )

    if not rows:
        print("未提取到费用行", file=sys.stderr)
        return 1

    print(f"{'来源文件':24} {'原始名称':34} {'标准名称':28} {'方式':8} {'分数':5} {'金额':>9}")
    print("-" * 116)
    for name, raw, standard, method, score, amount in rows:
        flag = "" if method in {"exact", "alias"} else " ⚠"
        print(
            f"{name[:24]:24} {raw[:34]:34} {standard[:28]:28} {method:8} {score:5.2f} {amount:>9,.2f}{flag}"
        )
    changed = sum(1 for row in rows if row[1].strip().lower() != row[2].strip().lower())
    review = sum(1 for row in rows if row[3] not in {"exact", "alias"})
    print("-" * 116)
    print(f"共 {len(rows)} 条费用；被重命名 {changed} 条；需人工确认 {review} 条")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    client = OcrClient(SETTINGS)
    try:
        if str(args.source).startswith(("http://", "https://")):
            response = client.process_url(args.source)
        else:
            response = client.process_file(args.source, force=args.force)
    except OcrError as exc:
        print(f"OCR 失败: {exc}", file=sys.stderr)
        return 1

    print(f"# url={response.url} cached={response.cached} elapsed={response.elapsed:.1f}s")
    for line in render_ocr_text(response.payload):
        print(line)
    charges = find_charges(response.payload)
    print(f"# 检出费用行 {len(charges)} 条")
    if args.out:
        Path(args.out).write_text(
            json.dumps(response.payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    root = Path(args.dir or "samples")
    store_path = root / "existing_bills.json"
    files = [p for p in sorted(root.iterdir()) if p.suffix.lower() in {".pdf", ".xlsx", ".docx", ".eml"}]
    if not files:
        print("samples/ 为空，请先运行 python scripts/make_samples.py", file=sys.stderr)
        return 1
    store = BillStore.from_file(store_path)
    result = run_paths(files, store=store, use_llm=False)
    print(f"{'文件':28} {'操作':14} {'费用':32} {'金额':>10} {'币种':6} {'AR/AP':6}")
    print("-" * 104)
    for record, extra in zip(result.records, result.extras, strict=True):
        charge = (record.charge_details.charge_item or "-")[:32]
        amount = record.charge_details.amount
        print(
            f"{Path(extra['source_file']).name[:28]:28} {record.operation.value:14} {charge:32} "
            f"{('' if amount is None else f'{amount:,.2f}'):>10} {record.charge_details.currency or '-':6} "
            f"{record.financial_attributes.posting_type or '-':6}"
        )
    summary = summarize(result.records)
    print("-" * 104)
    print(f"共 {summary['records']} 条记录，分布 {summary['by_operation']}，待复核 {summary['needs_review']} 条")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from harness.run_eval import main as eval_main

    return eval_main(args.dataset, verbose=args.verbose)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sailbill",
        description="航运智能账单助手：多格式解析 -> 抽取 -> 标准化 -> 匹配 -> AR/AP -> 标准 JSON",
    )
    parser.add_argument("--version", action="version", version=f"sailbill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="处理单据并输出标准 JSON")
    run_p.add_argument("files", nargs="+", help="文件或目录（EML/PDF/图片/Excel/Word）")
    run_p.add_argument("--store", default="samples/existing_bills.json", help="已有账单 JSON")
    run_p.add_argument("--out", default=None, help="输出 JSON 文件")
    run_p.add_argument("--trace", default=None, help="输出 trace JSON 文件")
    run_p.add_argument("--entity", default=None, help="记账主体（默认从账单库推断）")
    run_p.add_argument("--extended", action="store_true", help="附加置信度/证据/匹配细节")
    run_p.add_argument("--offline", action="store_true", help="不发起任何网络调用")
    run_p.add_argument("--no-llm", action="store_true", help="只用规则引擎")
    run_p.add_argument("--hitl", action="store_true", help="在写账前暂停（LangGraph interrupt），等待人工确认")
    run_p.add_argument("--commit", action="store_true", help="把 CREATE/UPDATE 结果写回账单库")
    run_p.add_argument("--save-bills", default=None, help="配合 --commit，把更新后的账单库另存为文件")
    run_p.add_argument("--quiet", action="store_true", help="不打印 JSON 到标准输出")
    run_p.set_defaults(func=cmd_run)

    names_p = sub.add_parser("names", help="展示费用名称的原始写法与标准化结果（题面四.3）")
    names_p.add_argument("files", nargs="+", help="文件或目录")
    names_p.add_argument("--offline", action="store_true", help="不发起任何网络调用")
    names_p.set_defaults(func=cmd_names)

    ocr_p = sub.add_parser("ocr", help="单独调用 OCR 服务并查看结构化结果")
    ocr_p.add_argument("source", help="本地文件或公开 URL")
    ocr_p.add_argument("--out", default=None, help="保存原始响应")
    ocr_p.add_argument("--force", action="store_true", help="忽略缓存")
    ocr_p.set_defaults(func=cmd_ocr)

    demo_p = sub.add_parser("demo", help="用内置样本跑一遍全链路")
    demo_p.add_argument("--dir", default=None, help="样本目录，默认 samples/")
    demo_p.set_defaults(func=cmd_demo)

    eval_p = sub.add_parser("eval", help="运行金标评测")
    eval_p.add_argument("--dataset", default="eval/golden", help="金标集目录")
    eval_p.add_argument("--verbose", action="store_true")
    eval_p.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # top-level guard: one clean error, no traceback dump
        if getattr(args, "verbose", False):
            raise
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
