"""Загрузка и доступ к конфигурации проекта."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from .utils import project_root


@dataclass
class Config:
    """Собранная конфигурация: settings + маппинги + секреты из .env."""

    root: Path
    settings: Dict[str, Any]
    mapping_1c: Dict[str, Any]
    mapping_bitrix: Dict[str, Any]
    bitrix_webhook_url: Optional[str]

    # ── пути ────────────────────────────────────────────────────────────────
    def _path(self, key: str, default: str) -> Path:
        rel = self.settings.get("paths", {}).get(key, default)
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self._path("db", "data/db/applicants.sqlite")

    @property
    def input_dir(self) -> Path:
        return self._path("input_dir", "data/input")

    @property
    def output_dir(self) -> Path:
        return self._path("output_dir", "data/output")

    # ── статусы ───────────────────────────────────────────────────────────────
    @property
    def status_order(self) -> List[str]:
        return self.settings.get("statuses", {}).get("order", [])

    @property
    def default_status(self) -> str:
        return self.settings.get("statuses", {}).get("default", "lead")

    def canonical_status(self, raw: Optional[str]) -> str:
        """Привести «сырой» статус из источника к каноническому."""
        if not raw:
            return self.default_status
        mapping = self.settings.get("status_mapping", {})
        key = str(raw).strip().lower()
        if key in mapping:
            return mapping[key]
        # если raw уже является каноническим — оставляем
        if key in self.status_order:
            return key
        return self.default_status

    # ── загрузка ──────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, root: Optional[Path] = None, config_dir: Optional[Path] = None) -> "Config":
        root = root or project_root()
        config_dir = config_dir or Path(os.environ.get("ADMISSIONS_CONFIG_DIR", root / "config"))

        load_dotenv(root / ".env")

        return cls(
            root=root,
            settings=_load_yaml(config_dir / "settings.yaml"),
            mapping_1c=_load_yaml(config_dir / "mapping_1c.yaml"),
            mapping_bitrix=_load_yaml(config_dir / "mapping_bitrix.yaml"),
            bitrix_webhook_url=os.environ.get("BITRIX_WEBHOOK_URL"),
        )


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
