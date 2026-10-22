"""OCR client + tolerant adapter for the assigned OCR service.

Two things make this harder than a ``requests.post``:

1. The service takes a **URL**, not bytes -- it downloads the file itself.  A
   local file therefore needs a delivery strategy (see :class:`UrlResolver`).
   The same wrinkle is the assignment's biggest practical trap.
2. The response is *domain structured* JSON (``data.list[].chargeList[]`` ...)
   rather than raw text with bounding boxes, and its schema is neither
   documented nor stable across document types.  The adapter is therefore
   written defensively: it maps keys through a synonym table, keeps every raw
   payload, and renders a readable text view that later stages can cite as
   evidence.

Responses are cached by file hash so a rerun costs nothing and tests stay
offline and deterministic.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import SETTINGS, Settings
from ..models import NormalizedDocument, sha256_file


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class OcrError(RuntimeError):
    """Base class for OCR failures."""


class OcrUnavailable(OcrError):
    """No endpoint configured or delivery disabled."""


class OcrDeliveryError(OcrError):
    """Could not turn a local file into a URL the service can download."""


# --------------------------------------------------------------------------- #
# local file -> URL
# --------------------------------------------------------------------------- #
class UrlResolver:
    """Resolve a local path to a URL the OCR service can fetch."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or SETTINGS
        self._map_path = self.settings.cache_dir / "urls.json"
        self._map: dict[str, str] = self._load_map()

    def _load_map(self) -> dict[str, str]:
        if self._map_path.exists():
            try:
                return json.loads(self._map_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_map(self) -> None:
        self._map_path.parent.mkdir(parents=True, exist_ok=True)
        self._map_path.write_text(json.dumps(self._map, indent=0), encoding="utf-8")

    def resolve(self, path: str | Path, *, force_new: bool = False) -> str:
        p = Path(path)
        if str(p).startswith(("http://", "https://")):
            return str(p)

        sha = sha256_file(p)
        if not force_new and sha in self._map:
            return self._map[sha]

        mode = self.settings.file_delivery
        url: str | None = None
        if mode in {"public", "auto"} and self.settings.public_base_url:
            url = self.settings.public_base_url.rstrip("/") + "/" + quote(p.name)
        if url is None and mode in {"upload", "auto"} and self.settings.upload_endpoint:
            url = self._upload(p)
        if url is None:
            raise OcrDeliveryError(
                "OCR 服务需要一个可公开下载的 URL，但当前没有可用的投递方式。\n"
                "任选其一：\n"
                "  1) SAILBILL_FILE_DELIVERY=public + SAILBILL_PUBLIC_BASE_URL=<你的静态站点>\n"
                "  2) SAILBILL_FILE_DELIVERY=upload + SAILBILL_UPLOAD_ENDPOINT=<文件托管 API>\n"
                "  3) 直接用 --url 传入文档的公开链接\n"
                "  4) SAILBILL_FILE_DELIVERY=none 走离线录制回放"
            )
        self._map[sha] = url
        self._save_map()
        return url

    def invalidate(self, path: str | Path) -> None:
        sha = sha256_file(path)
        if self._map.pop(sha, None):
            self._save_map()

    def _upload(self, path: Path) -> str:
        import requests

        data = path.read_bytes()
        files = {"file": (path.name, data, "application/octet-stream")}
        resp = requests.post(self.settings.upload_endpoint, files=files, timeout=(10, 120))
        resp.raise_for_status()
        payload = resp.json()
        for key in ("link", "url", "download_url", "downloadUrl", "file"):
            val = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(val, str) and val.startswith("http"):
                return val
        raise OcrDeliveryError(f"上传成功但响应中没有下载链接: {str(payload)[:300]}")


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
@dataclass
class OcrResponse:
    payload: dict[str, Any]
    url: str = ""
    cached: bool = False
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)


class OcrClient:
    def __init__(self, settings: Settings | None = None, resolver: UrlResolver | None = None) -> None:
        self.settings = settings or SETTINGS
        self.resolver = resolver or UrlResolver(self.settings)

    # -- public ----------------------------------------------------------
    def process_file(self, path: str | Path, *, force: bool = False) -> OcrResponse:
        p = Path(path)
        sha = sha256_file(p)
        cache_file = self.settings.cache_dir / "ocr" / f"{sha}.json"

        if self.settings.ocr_cache and cache_file.exists() and not force:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            return OcrResponse(payload=payload, cached=True)

        url = self.resolver.resolve(p)
        started = time.time()
        try:
            payload = self._post(url)
        except OcrError as exc:
            # a stale single-use link is the most likely cause: retry once
            if "下载" in str(exc) or "download" in str(exc).lower():
                self.resolver.invalidate(p)
                url = self.resolver.resolve(p, force_new=True)
                payload = self._post(url)
            else:
                raise
        elapsed = time.time() - started

        if self.settings.ocr_cache:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return OcrResponse(payload=payload, url=url, elapsed=elapsed)

    def process_url(self, url: str) -> OcrResponse:
        started = time.time()
        payload = self._post(url)
        return OcrResponse(payload=payload, url=url, elapsed=time.time() - started)

    # -- internals -------------------------------------------------------
    def _post(self, url: str) -> dict[str, Any]:
        import requests

        if not self.settings.ocr_endpoint:
            raise OcrUnavailable("未配置 SAILBILL_OCR_ENDPOINT")
        body = {
            "file": url,
            "task": self.settings.ocr_task,
            "callBack": "",
            "taskType": "",
        }
        endpoint = self.settings.ocr_endpoint.rstrip("/") + "/process"
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    endpoint, json=body, timeout=(10, self.settings.ocr_timeout)
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise OcrError(f"OCR 返回失败: {payload.get('message')} ({payload.get('error_code')})")
                return payload
            except OcrError:
                raise
            except Exception as exc:  # network / json
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise OcrError(f"OCR 调用失败: {last_exc}")


# --------------------------------------------------------------------------- #
# tolerant adapter
# --------------------------------------------------------------------------- #
_CHARGE_FIELDS: dict[str, tuple[str, ...]] = {
    "charge_item_raw": (
        "chargename", "charge_name", "chargeitem", "charge", "feename", "fee_name", "fee",
        "item", "description", "desc", "name", "chargetype", "费用名称", "费用", "项目",
    ),
    "amount": (
        "amount", "amt", "feeamount", "totalamount", "total", "sum", "value", "price",
        "chargeamount", "金额", "总金额", "费用金额",
    ),
    "currency": ("currency", "currencycode", "ccy", "curr", "币种", "货币"),
    "quantity": ("quantity", "qty", "count", "数量"),
    "unit_price": ("unitprice", "unit_price", "rate", "unitrate", "单价"),
    "payment_term": ("paymentterm", "payment", "ppcc", "prepaidcollect", "付款方式", "运费条款"),
    "remark": ("remark", "note", "comment", "备注"),
}

_CHARGE_LIST_KEYS = (
    "chargelist", "charge_list", "charges", "feedetail", "feedetails", "fee_list",
    "feemx", "feemxlist", "chargesdetail", "费用明细", "费用列表",
)

_REFERENCE_KEYS = {
    "bl_no": ("mblnumber", "mbl_no", "mblno", "blnumber", "bl_no", "blno", "billoflding", "billoflading"),
    "hbl_no": ("hblnumber", "hbl_no", "hblno"),
    "booking_no": ("bookingnumber", "booking_no", "bookingno", "sonumber", "so_no"),
    "container_no": ("ctrlist", "containers", "containerno", "containernumbers", "cntr"),
    "voyage_no": ("voyagenumber", "voyage_no", "vesselvoyage", "voyage"),
}

_PARTY_KEYS = {
    "issuer": ("shipper", "issuer", "sender", "billfrom", "签发方"),
    "recipient": ("consignee", "recipient", "receiver", "billto", "接收方"),
}


def _norm_key(key: str) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _iter_dicts(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 8:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item, depth + 1)


def _find_list(node: Any, keys: tuple[str, ...], depth: int = 0) -> list[Any] | None:
    if depth > 8:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if _norm_key(k) in keys and isinstance(v, list):
                return v
        for v in node.values():
            found = _find_list(v, keys, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_list(item, keys, depth + 1)
            if found:
                return found
    return None


def _lookup(mapping: dict[str, Any], names: tuple[str, ...]) -> Any:
    normalised = {_norm_key(k): v for k, v in mapping.items()}
    for name in names:
        if name in normalised and normalised[name] not in (None, "", [], {}):
            return normalised[name]
    return None


def find_charges(payload: Any) -> list[dict[str, Any]]:
    """Pull candidate charge dictionaries out of an arbitrary OCR payload."""
    charges = _find_list(payload, _CHARGE_LIST_KEYS)
    out: list[dict[str, Any]] = []
    if isinstance(charges, list):
        for entry in charges:
            if not isinstance(entry, dict):
                continue
            row = {field: _lookup(entry, names) for field, names in _CHARGE_FIELDS.items()}
            row["_raw"] = entry
            if any(row.get(k) for k in ("charge_item_raw", "amount")):
                out.append(row)
    return out


def find_references(payload: Any) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for node in _iter_dicts(payload):
        for kind, names in _REFERENCE_KEYS.items():
            value = _lookup(node, names)
            if not value:
                continue
            values: list[str] = []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            if isinstance(v, str) and v.strip() and _norm_key(k) not in {"pagetype"}:
                                values.append(v.strip())
                    elif isinstance(item, str):
                        values.append(item.strip())
            elif isinstance(value, str):
                values.append(value.strip())
            for v in values:
                if v and len(v) <= 40 and v not in refs.setdefault(kind, []):
                    refs[kind].append(v)
    return {k: v for k, v in refs.items() if v}


def find_parties(payload: Any) -> dict[str, Any]:
    parties: dict[str, Any] = {}
    for node in _iter_dicts(payload):
        for kind, names in _PARTY_KEYS.items():
            if kind in parties:
                continue
            value = _lookup(node, names)
            if isinstance(value, str) and value.strip():
                parties[kind] = value.strip()
    return parties


def find_doc_meta(payload: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for node in _iter_dicts(payload):
        for kind, names in {
            "document_type": ("filetype", "doctype", "documenttype", "typename", "单据类型"),
            "invoice_no": ("invoicenumber", "invoiceno", "invoice_number", "发票号"),
            "created": ("createdate", "date", "documentdate", "日期"),
        }.items():
            if kind in meta:
                continue
            value = _lookup(node, names)
            if isinstance(value, str) and value.strip():
                meta[kind] = value.strip()
    return meta


def render_ocr_text(payload: Any, *, max_lines: int = 400) -> list[str]:
    """Render the structured payload as citable text lines."""
    lines: list[str] = []
    meta = find_doc_meta(payload)
    for key, value in meta.items():
        lines.append(f"[OCR] {key}: {value}")

    refs = find_references(payload)
    for key, values in refs.items():
        lines.append(f"[OCR] {key}: {', '.join(values[:8])}")

    parties = find_parties(payload)
    for key, value in parties.items():
        lines.append(f"[OCR] {key}: {value}")

    charges = find_charges(payload)
    for i, row in enumerate(charges, 1):
        parts = [f"{k}={row.get(k)}" for k in ("charge_item_raw", "quantity", "unit_price", "amount", "currency", "payment_term") if row.get(k)]
        lines.append(f"[OCR] charge#{i}: " + " | ".join(parts))

    if not lines:  # fall back to a shallow flattening so nothing is silently lost
        for node in _iter_dicts(payload):
            for k, v in node.items():
                if isinstance(v, (str, int, float)) and str(v).strip():
                    lines.append(f"[OCR] {k}: {v}")
                if len(lines) >= max_lines:
                    break
            if len(lines) >= max_lines:
                break
    return lines[:max_lines]


def attach_ocr(doc: NormalizedDocument, client: OcrClient) -> NormalizedDocument:
    """Run OCR for a doc that needs it and enrich it in place."""
    if doc.ocr is not None:
        return doc
    if not doc.path:
        return doc
    try:
        response = client.process_file(doc.path)
    except OcrError as exc:
        doc.warnings.append(f"OCR 失败: {exc}")
        doc.meta["ocr_error"] = str(exc)
        return doc

    payload = response.payload
    doc.ocr = payload if isinstance(payload, dict) else {"data": payload}
    doc.meta["ocr_url"] = response.url
    doc.meta["ocr_cached"] = response.cached
    doc.meta["ocr_elapsed"] = round(response.elapsed, 2)
    doc.meta["ocr_refs"] = find_references(payload)
    doc.meta["ocr_parties"] = find_parties(payload)
    doc.meta["ocr_meta"] = find_doc_meta(payload)

    text_lines = render_ocr_text(payload)
    for i, line in enumerate(text_lines):
        doc.add_block(line, kind="ocr", line=i, source=doc.filename)
    doc.meta["ocr_text"] = "\n".join(text_lines)
    if len(find_charges(payload)) == 0:
        doc.warnings.append("OCR 结果中未发现费用明细（chargeList 为空），需结合原文或人工复核")
    return doc
