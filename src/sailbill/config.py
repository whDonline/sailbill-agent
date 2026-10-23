"""Configuration, loaded from environment (and an optional ``.env``).

Every knob has a safe default so the agent starts with zero configuration:
without an API key it runs in deterministic ``rules`` mode, without network it
runs on recorded fixtures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover - dotenv is an optional convenience
    pass


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    llm_timeout: float = 90.0
    offline: bool = False

    # OCR
    ocr_endpoint: str = "http://36.103.199.11:5002"
    ocr_task: str = "orderChange"
    ocr_timeout: float = 300.0
    ocr_cache: bool = True
    ocr_force_for_pdf: bool = False  # OCR even when the PDF has a usable text layer

    # local file -> public URL
    file_delivery: str = "auto"  # auto | public | upload | none
    public_base_url: str = ""
    upload_endpoint: str = ""
    # multipart field name for the file part (uguu.se uses "files[]")
    upload_file_field: str = "file"
    # extra multipart form fields required by the upload service,
    # e.g. litterbox: "reqtype=fileupload,time=1h"
    upload_extra_fields: str = ""

    # decision thresholds
    review_threshold: float = 0.60
    amount_tolerance: float = 0.01

    # the entity the books are kept for; AR/AP cannot be decided without it
    accounting_entity: str = ""

    # infrastructure
    cache_dir: Path = Path(".cache/sailbill")
    max_attachment_depth: int = 3
    max_text_chars: int = 120_000

    # ------------------------------------------------------------------ #
    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and not self.offline

    @property
    def ocr_enabled(self) -> bool:
        return bool(self.ocr_endpoint) and self.file_delivery != "none" and not self.offline

    @classmethod
    def load(cls) -> Settings:
        return cls(
            llm_provider=_env("SAILBILL_LLM_PROVIDER", "deepseek"),
            llm_model=_env("SAILBILL_LLM_MODEL", "deepseek-flash"),
            llm_base_url=_env("SAILBILL_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            llm_api_key=_env("SAILBILL_LLM_API_KEY") or _env("DEEPSEEK_API_KEY") or _env("OPENAI_API_KEY"),
            llm_temperature=_float("SAILBILL_LLM_TEMPERATURE", 0.0),
            llm_timeout=_float("SAILBILL_LLM_TIMEOUT", 90.0),
            offline=_bool("SAILBILL_OFFLINE", False),
            ocr_endpoint=_env("SAILBILL_OCR_ENDPOINT", "http://36.103.199.11:5002"),
            ocr_task=_env("SAILBILL_OCR_TASK", "orderChange"),
            ocr_timeout=_float("SAILBILL_OCR_TIMEOUT", 300.0),
            ocr_cache=_bool("SAILBILL_OCR_CACHE", True),
            ocr_force_for_pdf=_bool("SAILBILL_OCR_FOR_PDF", False),
            file_delivery=_env("SAILBILL_FILE_DELIVERY", "auto") or "auto",
            public_base_url=_env("SAILBILL_PUBLIC_BASE_URL"),
            upload_endpoint=_env("SAILBILL_UPLOAD_ENDPOINT"),
            upload_file_field=_env("SAILBILL_UPLOAD_FILE_FIELD", "file") or "file",
            upload_extra_fields=_env("SAILBILL_UPLOAD_EXTRA_FIELDS"),
            review_threshold=_float("SAILBILL_CONFIDENCE_REVIEW", 0.60),
            amount_tolerance=_float("SAILBILL_AMOUNT_TOLERANCE", 0.01),
            accounting_entity=_env("SAILBILL_ACCOUNTING_ENTITY"),
            cache_dir=Path(_env("SAILBILL_CACHE_DIR") or ".cache/sailbill"),
            max_attachment_depth=int(_float("SAILBILL_MAX_ATTACHMENT_DEPTH", 3)),
            max_text_chars=int(_float("SAILBILL_MAX_TEXT_CHARS", 120_000)),
        )


SETTINGS = Settings.load()
