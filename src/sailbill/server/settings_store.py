"""桌面版配置层：config.json + 环境变量的合并读取。

优先级：环境变量（SAILBILL_*）> 数据目录下 ``config.json`` > 内置默认。
设置向导（Web 界面）写入 config.json，不碰环境变量，方便打包后零配置使用。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import Settings

# 可通过设置页写入的字段 -> (候选环境变量, 类型)
_CONFIGURABLE: dict[str, tuple[list[str], type]] = {
    "llm_api_key": (["SAILBILL_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"], str),
    "llm_model": (["SAILBILL_LLM_MODEL"], str),
    "llm_base_url": (["SAILBILL_LLM_BASE_URL"], str),
    "ocr_endpoint": (["SAILBILL_OCR_ENDPOINT"], str),
    "ocr_task": (["SAILBILL_OCR_TASK"], str),
    "ocr_timeout": (["SAILBILL_OCR_TIMEOUT"], float),
    "file_delivery": (["SAILBILL_FILE_DELIVERY"], str),
    "public_base_url": (["SAILBILL_PUBLIC_BASE_URL"], str),
    "upload_endpoint": (["SAILBILL_UPLOAD_ENDPOINT"], str),
    "upload_file_field": (["SAILBILL_UPLOAD_FILE_FIELD"], str),
    "upload_extra_fields": (["SAILBILL_UPLOAD_EXTRA_FIELDS"], str),
    "review_threshold": (["SAILBILL_CONFIDENCE_REVIEW"], float),
    "amount_tolerance": (["SAILBILL_AMOUNT_TOLERANCE"], float),
    "accounting_entity": (["SAILBILL_ACCOUNTING_ENTITY"], str),
    "offline": (["SAILBILL_OFFLINE"], bool),
}


def data_dir() -> Path:
    """桌面版数据目录：SAILBILL_DATA_DIR > %APPDATA%/sailbill > ~/.sailbill。"""
    override = os.getenv("SAILBILL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "sailbill"
    return Path.home() / ".sailbill"


def config_path() -> Path:
    return data_dir() / "config.json"


def read_config() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in raw.items() if k in _CONFIGURABLE}


def write_config(partial: dict[str, Any]) -> dict[str, Any]:
    """合并写入设置向导提交的字段，返回落盘后的完整配置。"""
    stored = read_config()
    for key, value in partial.items():
        if key not in _CONFIGURABLE:
            continue
        _, value_type = _CONFIGURABLE[key]
        stored[key] = _coerce(value, value_type)
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    return stored


def _coerce(value: Any, value_type: type) -> Any:
    if value_type is str:
        return "" if value is None else str(value).strip()
    if value_type is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if value_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    return value


def get_effective_settings() -> Settings:
    """合并配置源：env 显式设置的字段优先，其余用 config.json 兜底。"""
    base = Settings.load()
    changes: dict[str, Any] = {}
    for key, (env_keys, value_type) in _CONFIGURABLE.items():
        if any(os.getenv(env, "").strip() for env in env_keys):
            continue  # 环境变量显式设置，不覆盖
        stored = read_config().get(key)
        if stored is None:
            continue
        changes[key] = _coerce(stored, value_type)
    return replace(base, **changes) if changes else base


def public_view() -> dict[str, Any]:
    """给设置页的脱敏视图：API Key 只显示尾四位。"""
    settings = get_effective_settings()
    out: dict[str, Any] = {}
    for key in _CONFIGURABLE:
        value = getattr(settings, key)
        if key == "llm_api_key" and value:
            shown = str(value)
            value = "*" * 8 + shown[-4:] if len(shown) > 4 else "****"
        out[key] = value
    out["llm_enabled"] = settings.llm_enabled
    out["config_file"] = str(config_path())
    out["data_dir"] = str(data_dir())
    # OCR 公网投递风险提示（题目阶段用公网中转，正式使用建议自建中转端点）
    out["delivery_warning"] = (
        "file_delivery=upload 时单据文件会先上传到公网中转服务再交给 OCR 识别；"
        "正式使用请在下方填入公司自建的文件中转端点，避免敏感单据经过第三方。"
    )
    return out
