"""Мелкие вспомогательные функции, не привязанные к домену."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path


def project_root() -> Path:
    """Корень проекта admissions/ (на два уровня выше пакета src/admissions)."""
    return Path(__file__).resolve().parents[2]


def now_iso() -> str:
    """Текущее время в ISO-формате (локальная зона, секундная точность)."""
    return datetime.now().replace(microsecond=0).isoformat()


def utcnow_iso() -> str:
    """Текущее время UTC в ISO-формате."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def timestamp_slug() -> str:
    """Метка времени для имён файлов: 20260620_121530."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def file_sha256(path: str | Path) -> str:
    """SHA-256 содержимого файла — используется для идемпотентности загрузок."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def setup_logging(verbose: bool = False) -> None:
    """Единая настройка логирования для CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
