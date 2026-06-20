"""Загрузка данных из Битрикс24 (REST API) -> канонические записи.

Используется входящий вебхук (URL в .env, переменная BITRIX_WEBHOOK_URL).
Поддерживается пагинация методов crm.<entity>.list.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import Config
from .models import SOURCE_BITRIX, ApplicantRecord
from .normalize import clean_str, normalize_field

log = logging.getLogger("admissions")

_PAGE_SIZE = 50  # размер страницы в Битрикс24 фиксирован


def extract_value(field_value: Any) -> Any:
    """Достать скалярное значение из поля Битрикса.

    EMAIL/PHONE приходят списком [{"VALUE": "...", "VALUE_TYPE": "WORK"}, ...]
    — берём первое значение. Простые поля возвращаются как есть.
    """
    if isinstance(field_value, list):
        if not field_value:
            return None
        first = field_value[0]
        if isinstance(first, dict):
            return first.get("VALUE")
        return first
    if isinstance(field_value, dict):
        return field_value.get("VALUE")
    return field_value


def map_raw_to_record(raw: Dict[str, Any], cfg: Config) -> ApplicantRecord:
    """Преобразовать одну запись Битрикса в нормализованный ApplicantRecord."""
    mapping = cfg.mapping_bitrix
    fields_map: Dict[str, str] = mapping.get("fields", {})
    id_field = mapping.get("id_field", "ID")
    status_field = mapping.get("status_field", "STATUS_ID")

    fields: Dict[str, Any] = {}
    for canonical, code in fields_map.items():
        if code in raw:
            fields[canonical] = normalize_field(canonical, extract_value(raw.get(code)))

    status_raw = clean_str(extract_value(raw.get(status_field)))
    status = cfg.canonical_status(status_raw)
    source_key = clean_str(extract_value(raw.get(id_field))) or ""

    return ApplicantRecord(
        source=SOURCE_BITRIX,
        source_key=str(source_key),
        fields=fields,
        status_raw=status_raw,
        status=status,
        raw=raw,
    )


def records_from_raw(raw_list: List[Dict[str, Any]], cfg: Config) -> List[ApplicantRecord]:
    """Преобразовать список сырых записей Битрикса в канонические."""
    return [map_raw_to_record(r, cfg) for r in raw_list]


class BitrixClient:
    """Минимальный клиент REST API Битрикс24 через входящий вебхук."""

    def __init__(self, webhook_url: str, entity: str = "lead",
                 select: Optional[List[str]] = None, flt: Optional[Dict[str, Any]] = None,
                 timeout: int = 30):
        if not webhook_url:
            raise ValueError(
                "Не задан BITRIX_WEBHOOK_URL. Скопируйте .env.example в .env и заполните."
            )
        self.base = webhook_url.rstrip("/") + "/"
        self.entity = entity
        self.select = select or []
        self.filter = flt or {}
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: Config) -> "BitrixClient":
        m = cfg.mapping_bitrix
        return cls(
            webhook_url=cfg.bitrix_webhook_url or "",
            entity=m.get("entity", "lead"),
            select=m.get("select") or [],
            flt=m.get("filter") or {},
        )

    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base + method + ".json"
        resp = requests.post(url, json=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Битрикс вернул ошибку: {data.get('error')} {data.get('error_description', '')}")
        return data

    def list_all(self) -> List[Dict[str, Any]]:
        """Выгрузить все записи сущности с учётом пагинации."""
        method = f"crm.{self.entity}.list"
        params: Dict[str, Any] = {"start": 0}
        if self.select:
            params["select"] = self.select
        if self.filter:
            params["filter"] = self.filter

        results: List[Dict[str, Any]] = []
        start = 0
        while True:
            params["start"] = start
            data = self._call(method, params)
            batch = data.get("result", []) or []
            results.extend(batch)
            log.debug("Битрикс: получено %d (всего %d)", len(batch), len(results))
            nxt = data.get("next")
            if nxt is None or not batch:
                break
            start = nxt
        return results

    def get_fields(self) -> Dict[str, Any]:
        """Метаданные полей сущности (crm.<entity>.fields)."""
        data = self._call(f"crm.{self.entity}.fields", {})
        return data.get("result", {}) or {}

    def print_inspection(self) -> None:
        """Вывести доступные поля сущности и пример записи (этап разведки)."""
        print(f"\nСущность Битрикса: {self.entity}")
        print("── Поля (код: тип / название) ───────────────────────────")
        fields = self.get_fields()
        for code in sorted(fields):
            meta = fields[code] or {}
            title = meta.get("title") or meta.get("formLabel") or ""
            ftype = meta.get("type", "")
            print(f"  {code}: {ftype} / {title}")

        sample = self.list_all()[:1]
        if sample:
            print("\n── Пример записи ────────────────────────────────────────")
            print(json.dumps(sample[0], ensure_ascii=False, indent=2)[:2000])
        print()


def fetch_records(cfg: Config, from_file: Optional[str] = None) -> List[ApplicantRecord]:
    """Получить записи из Битрикса (через API) или из локального JSON (для офлайна/тестов)."""
    if from_file:
        raw_list = json.loads(Path(from_file).read_text(encoding="utf-8"))
        log.info("Битрикс: загружено из файла %s (%d записей)", from_file, len(raw_list))
    else:
        client = BitrixClient.from_config(cfg)
        raw_list = client.list_all()
    return records_from_raw(raw_list, cfg)
