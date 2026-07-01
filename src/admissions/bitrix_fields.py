"""Создание и резолв пользовательских полей контакта и сделки под ПК.

Поля помечены стабильным XML_ID (PK_*), чтобы операции были идемпотентными:
повторный запуск не плодит дубликаты. Названия полей в Битриксе совпадают с
названиями колонок выгрузки 1С — оператор видит знакомые подписи.

ВАЖНО: фактический код поля (FIELD_NAME) Битрикс может назначить сам
(особенно у контактов — UF_CRM_<n>), поэтому код всегда резолвится по XML_ID
через *.userfield.list, а не хардкодится.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .bitrix_client import BitrixClient
from .config import Config

log = logging.getLogger("admissions")

# Поля сделки бакалавриата. Подписи = названия колонок 1С с префиксом «Б:»
# (под будущие воронки магистратуры «М:» и аспирантуры «А:»). ФИО НЕ храним на
# сделке — оно живёт на контакте. Все поля авто-заполняемые, необязательные.
DESIRED_DEAL_FIELDS: List[Dict[str, Any]] = [
    {"xml": "B_CODE",     "name": "UF_CRM_B_CODE",     "type": "string",  "label": "Б: Уникальный код"},
    {"xml": "B_GROUP",    "name": "UF_CRM_B_GROUP",    "type": "string",  "label": "Б: Конкурсная группа"},
    {"xml": "B_BVI",      "name": "UF_CRM_B_BVI",      "type": "string",  "label": "Б: Без вступительных испытаний"},
    {"xml": "B_SCORE",    "name": "UF_CRM_B_SCORE",    "type": "double",  "label": "Б: Сумма баллов"},
    {"xml": "B_PRIORITY", "name": "UF_CRM_B_PRIORITY", "type": "integer", "label": "Б: Приоритет"},
    {"xml": "B_SCORE_ID", "name": "UF_CRM_B_SCORE_ID", "type": "double",  "label": "Б: Сумма баллов по ИД (все)"},
    {"xml": "B_BASIS",    "name": "UF_CRM_B_BASIS",    "type": "string",  "label": "Б: Основание поступления"},
    {"xml": "B_TARGETED", "name": "UF_CRM_B_TARGETED", "type": "string",  "label": "Б: Целевик"},
    {"xml": "B_CONSENT",  "name": "UF_CRM_B_CONSENT",  "type": "string",  "label": "Б: Согласие на зачисление"},
    {"xml": "B_SPECIAL",  "name": "UF_CRM_B_SPECIAL",  "type": "string",  "label": "Б: Лицо, имеющее особое право"},
    {"xml": "B_APP_DATE", "name": "UF_CRM_B_APP_DATE", "type": "string",  "label": "Б: Дата подачи заявления"},
    {"xml": "B_FEATURES", "name": "UF_CRM_B_FEATURES", "type": "string",  "label": "Б: Особенности приема"},
    {"xml": "B_CONTROL",  "name": "UF_CRM_B_CONTROL",  "type": "string",  "label": "Б: Контроль пройден"},
    {"xml": "B_UPDATED",  "name": "UF_CRM_B_UPDATED",  "type": "string",  "label": "Б: Обновлено"},
]

# Уровень поступающего храним в стандартном «Тип контакта» (TYPE_ID), не в UF.
DESIRED_CONTACT_FIELDS: List[Dict[str, Any]] = [
    {"xml": "PK_CODE", "name": "UF_CRM_PK_CODE", "type": "string", "label": "Уникальный код",
     "filter": True, "searchable": True},
]


def _userfield_list(client: BitrixClient, entity: str) -> List[Dict[str, Any]]:
    return client._call(f"crm.{entity}.userfield.list", {}).get("result", []) or []


def index_by_xml(client: BitrixClient, entity: str) -> Dict[str, Dict[str, Any]]:
    """Все пользовательские поля сущности, индекс по XML_ID (полные строки)."""
    rows = _userfield_list(client, entity)
    return {r.get("XML_ID"): r for r in rows if r.get("XML_ID")}


def _desired_for(entity: str) -> List[Dict[str, Any]]:
    return DESIRED_CONTACT_FIELDS if entity == "contact" else DESIRED_DEAL_FIELDS


def plan_fields(client: BitrixClient, entity: str
                ) -> List[Tuple[str, Dict[str, Any], Optional[str]]]:
    """Для каждого желаемого поля: ('exists'|'create', описание, фактический код)."""
    rows = _userfield_list(client, entity)
    by_xml = {r.get("XML_ID"): r for r in rows if r.get("XML_ID")}
    by_name = {r.get("FIELD_NAME"): r for r in rows}
    plan = []
    for d in _desired_for(entity):
        found = by_xml.get(d["xml"]) or by_name.get(d["name"])
        plan.append(("exists" if found else "create", d, found.get("FIELD_NAME") if found else None))
    return plan


def ensure_fields(client: BitrixClient, entity: str, apply: bool = False
                  ) -> Dict[str, Dict[str, Any]]:
    """Создать недостающие поля (если apply). Вернуть индекс по XML_ID (полные строки)."""
    if apply:
        for action, d, _ in plan_fields(client, entity):
            if action != "create":
                continue
            fields = {
                "FIELD_NAME": d["name"],
                "USER_TYPE_ID": d["type"],
                "XML_ID": d["xml"],
                "EDIT_FORM_LABEL": d["label"],
                "LIST_COLUMN_LABEL": d["label"],
                "LIST_FILTER_LABEL": d["label"],
                "MANDATORY": "N",
                "SHOW_IN_LIST": "Y",
                "SHOW_FILTER": "Y" if d.get("filter") else "N",
                "IS_SEARCHABLE": "Y" if d.get("searchable") else "N",
            }
            if d["type"] == "enumeration" and d.get("options"):
                fields["LIST"] = [{"VALUE": v} for v in d["options"]]
            res = client._call(f"crm.{entity}.userfield.add", {"fields": fields})
            if "error" in res:
                log.error("поле %s: %s", d["name"], res.get("error_description") or res.get("error"))
            else:
                log.info("создано поле %s (%s) у %s", d["name"], d["label"], entity)
    return index_by_xml(client, entity)


def resolve_codes(client: BitrixClient, entity: str) -> Dict[str, str]:
    """Карта XML_ID -> фактический FIELD_NAME для сущности."""
    return {xml: row.get("FIELD_NAME") for xml, row in index_by_xml(client, entity).items()}


def enum_value_map(field_row: Dict[str, Any]) -> Dict[str, str]:
    """Для enum-поля вернуть {VALUE -> ID} (для записи значения в Битрикс)."""
    out: Dict[str, str] = {}
    for item in field_row.get("LIST") or []:
        if item.get("VALUE") is not None:
            out[item["VALUE"]] = str(item.get("ID"))
    return out


# ── обёртки для CLI ──────────────────────────────────────────────────────────

def _setup(cfg: Config, entity: str, apply: bool):
    client = BitrixClient.from_config(cfg)
    plan = plan_fields(client, entity)  # план по состоянию ДО создания
    if apply:
        ensure_fields(client, entity, apply=True)
    codes = resolve_codes(client, entity) if apply else {}
    return plan, codes


def setup_deal_fields(cfg: Config, apply: bool = False):
    return _setup(cfg, "deal", apply)


def setup_contact_fields(cfg: Config, apply: bool = False):
    return _setup(cfg, "contact", apply)
