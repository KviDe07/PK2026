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
    """Собранная конфигурация: settings + маппинги + секрет вебхука из .env."""

    root: Path
    settings: Dict[str, Any]
    mapping_1c: Dict[str, Any]
    mapping_deal: Dict[str, Any]
    mapping_contact: Dict[str, Any]
    bitrix_webhook_url: Optional[str]

    # ── пути ────────────────────────────────────────────────────────────────
    def _path(self, key: str, default: str) -> Path:
        rel = self.settings.get("paths", {}).get(key, default)
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def input_dir(self) -> Path:
        return self._path("input_dir", "data/input")

    @property
    def output_dir(self) -> Path:
        return self._path("output_dir", "data/output")

    # ── параметры воронки бакалавриата ───────────────────────────────────────
    @property
    def category_id(self) -> int:
        return int(self.settings.get("bakalavriat_category_id", 8))

    @property
    def contact_type_id(self) -> str:
        """Стандартный «Тип контакта» (TYPE_ID) для воронки: CLIENT=Абитуриенты и т.д."""
        return self.settings.get("contact_type_id", "CLIENT")

    @property
    def levels(self) -> Dict[str, Dict[str, Any]]:
        """Уровни поступления для веб-приложения (bachelor активен, маг/асп — задел)."""
        return self.settings.get("levels", {"bachelor": {"title": "Бакалавриат", "enabled": True}})

    @property
    def default_stage_id(self) -> Optional[str]:
        return self.settings.get("default_stage_id")

    @property
    def stage_on_application(self) -> str:
        """Стадия для новой сделки, когда заявление пришло без предыдущего контакта."""
        return self.settings.get("stages", {}).get("on_application", "Поступившие заявления")

    @property
    def stage_on_contacted(self) -> str:
        """Стадия при заполнении пустой сделки оператора (контакт уже был)."""
        return self.settings.get("stages", {}).get("on_contacted", "Связались")

    @property
    def stage_on_withdrawn(self) -> Optional[str]:
        """Стадия для отозванных заявлений (сделки нет в новой выгрузке). None = только отчёт."""
        return self.settings.get("stages", {}).get("on_withdrawn")

    @property
    def operator_protected(self) -> List[str]:
        return self.mapping_deal.get("operator_protected", ["STAGE_ID", "TITLE"])

    @property
    def columns_1c(self) -> Dict[str, str]:
        return self.mapping_1c.get("columns", {}) or {}

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
            mapping_deal=_load_yaml(config_dir / "mapping_deal.yaml", required=False),
            mapping_contact=_load_yaml(config_dir / "mapping_contact.yaml", required=False),
            bitrix_webhook_url=os.environ.get("BITRIX_WEBHOOK_URL"),
        )


def _load_yaml(path: Path, required: bool = True) -> Dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Не найден файл конфигурации: {path}")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
