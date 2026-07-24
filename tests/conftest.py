"""Общие фикстуры для тестов (без сети и БД)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from admissions.config import Config


@pytest.fixture
def cfg():
    """Реальная конфигурация проекта (config/*.yaml)."""
    return Config.load()


# Готовые пользовательские поля (как их вернул бы *.userfield.list).
_DEAL_UF = [
    {"XML_ID": d["xml"], "FIELD_NAME": d["name"]} for d in [
        {"xml": "B_CODE", "name": "UF_CRM_B_CODE"},
        {"xml": "B_GROUP", "name": "UF_CRM_B_GROUP"},
        {"xml": "B_BVI", "name": "UF_CRM_B_BVI"},
        {"xml": "B_SCORE", "name": "UF_CRM_B_SCORE"},
        {"xml": "B_PRIORITY", "name": "UF_CRM_B_PRIORITY"},
        {"xml": "B_SCORE_ID", "name": "UF_CRM_B_SCORE_ID"},
        {"xml": "B_BASIS", "name": "UF_CRM_B_BASIS"},
        {"xml": "B_TARGETED", "name": "UF_CRM_B_TARGETED"},
        {"xml": "B_CONSENT", "name": "UF_CRM_B_CONSENT"},
        {"xml": "B_SPECIAL", "name": "UF_CRM_B_SPECIAL"},
        {"xml": "B_APP_DATE", "name": "UF_CRM_B_APP_DATE"},
        {"xml": "B_FEATURES", "name": "UF_CRM_B_FEATURES"},
        {"xml": "B_CONTROL", "name": "UF_CRM_B_CONTROL"},
        {"xml": "B_UPDATED", "name": "UF_CRM_B_UPDATED"},
        # магистратура «М:» (нет БВИ/Целевик/особое право/контроль; Кафедра — операторская)
        {"xml": "M_CODE", "name": "UF_CRM_M_CODE"},
        {"xml": "M_GROUP", "name": "UF_CRM_M_GROUP"},
        {"xml": "M_SCORE", "name": "UF_CRM_M_SCORE"},
        {"xml": "M_PRIORITY", "name": "UF_CRM_M_PRIORITY"},
        {"xml": "M_SCORE_ID", "name": "UF_CRM_M_SCORE_ID"},
        {"xml": "M_BASIS", "name": "UF_CRM_M_BASIS"},
        {"xml": "M_CONSENT", "name": "UF_CRM_M_CONSENT"},
        {"xml": "M_FEATURES", "name": "UF_CRM_M_FEATURES"},
        {"xml": "M_UPDATED", "name": "UF_CRM_M_UPDATED"},
        # аспирантура «А:» (нет БВИ/Целевик/особое право/контроль; УГС — готовое поле)
        {"xml": "A_CODE", "name": "UF_CRM_A_CODE"},
        {"xml": "A_GROUP", "name": "UF_CRM_A_GROUP"},
        {"xml": "A_SCORE", "name": "UF_CRM_A_SCORE"},
        {"xml": "A_PRIORITY", "name": "UF_CRM_A_PRIORITY"},
        {"xml": "A_SCORE_ID", "name": "UF_CRM_A_SCORE_ID"},
        {"xml": "A_BASIS", "name": "UF_CRM_A_BASIS"},
        {"xml": "A_CONSENT", "name": "UF_CRM_A_CONSENT"},
        {"xml": "A_FEATURES", "name": "UF_CRM_A_FEATURES"},
        {"xml": "A_APP_DATE", "name": "UF_CRM_A_APP_DATE"},
        {"xml": "A_UPDATED", "name": "UF_CRM_A_UPDATED"},
    ]
] + [
    # ГОТОВОЕ поле портала (заведено админом, без XML_ID): «Укрупнённая группа
    # специальностей» — enum-справочник, sync пишет в него по коду из ready_fields.
    {"XML_ID": None, "FIELD_NAME": "UF_CRM_1750624562799", "USER_TYPE_ID": "enumeration",
     "LIST": [
         {"ID": "44", "VALUE": "Математика и механика"},
         {"ID": "46", "VALUE": "Компьютерные науки и информатика"},
         {"ID": "48", "VALUE": "Информационные технологии и телекоммуникации"},
         {"ID": "50", "VALUE": "Машиностроение"},
         {"ID": "52", "VALUE": "Науки о Земле и окружающей среде"},
     ]},
]
_CONTACT_UF = [
    {"XML_ID": "PK_CODE", "FIELD_NAME": "UF_CRM_PK_CODE"},
]
_CONTACT_TYPES = [
    {"STATUS_ID": "CLIENT", "NAME": "Абитуриенты"},
    {"STATUS_ID": "SUPPLIER", "NAME": "Магистры"},
    {"STATUS_ID": "PARTNER", "NAME": "Аспиранты"},
]

# crm.deal.fields — подписи и справочники (для export: enum ID → текст).
_DEAL_FIELDS_META = {
    "UF_CRM_1750624562799": {"type": "enumeration", "items": [
        {"ID": "44", "VALUE": "Математика и механика"},
        {"ID": "46", "VALUE": "Компьютерные науки и информатика"},
        {"ID": "48", "VALUE": "Информационные технологии и телекоммуникации"},
        {"ID": "50", "VALUE": "Машиностроение"},
        {"ID": "52", "VALUE": "Науки о Земле и окружающей среде"},
    ]},
    "UF_CRM_1750780247059": {"type": "enumeration", "items": []},   # Кафедра
    "UF_CRM_1750624913519": {"type": "enumeration", "items": []},   # Специальность
    "UF_CRM_1752747663": {"type": "string"},   # Группа собеседование
    "UF_CRM_1752747696": {"type": "string"},   # Комментарий собеседование
}


class FakeBitrixClient:
    """In-memory заглушка BitrixClient: хранит контакты/сделки, пишет журнал."""

    def __init__(self, contacts: Optional[List[Dict[str, Any]]] = None,
                 deals: Optional[List[Dict[str, Any]]] = None,
                 stages: Optional[List[Dict[str, Any]]] = None,
                 reject_codes: Optional[set] = None,
                 comments: Optional[Dict] = None):
        self.contacts = contacts or []
        self.deals = deals or []
        self.comments = comments or {}   # {(entity_type, entity_id): [raw comments]}
        self.stages = stages or [
            {"STATUS_ID": "C8:UC_K4L2XI", "SORT": "20", "NAME": "Ждём заявление, приходили лично"},
            {"STATUS_ID": "C8:NEW", "SORT": "30", "NAME": "Поступившие заявления"},
            {"STATUS_ID": "C8:PREPARATION", "SORT": "40", "NAME": "Связались"},
        ]
        self.reject_codes = reject_codes or set()  # коды, чьё создание Битрикс «отклоняет» (фантом)
        self.writes: List[Tuple[str, Dict[str, Any]]] = []
        self._next_id = 1000

    def _find(self, store, _id):
        return next((x for x in store if str(x.get("ID")) == str(_id)), None)

    # ── чтение ──────────────────────────────────────────────────────────────
    def list_all(self, method: str, filter=None, select=None) -> List[Dict[str, Any]]:
        if method == "crm.contact.list":
            return list(self.contacts)
        if method == "crm.deal.list":
            cat = (filter or {}).get("CATEGORY_ID")
            return [d for d in self.deals if cat is None or str(d.get("CATEGORY_ID")) == str(cat)]
        return []

    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "crm.deal.userfield.list":
            return {"result": _DEAL_UF}
        if method == "crm.contact.userfield.list":
            return {"result": _CONTACT_UF}
        if method == "crm.status.list":
            if params.get("filter", {}).get("ENTITY_ID") == "CONTACT_TYPE":
                return {"result": _CONTACT_TYPES}
            return {"result": self.stages}
        if method == "crm.deal.fields":
            return {"result": _DEAL_FIELDS_META}
        raise AssertionError(f"_call неожиданно вызван: {method} {params}")

    # ── запись ──────────────────────────────────────────────────────────────
    def batch_call(self, operations: Sequence[Tuple[str, Dict[str, Any]]], halt: int = 0):
        out = []
        for method, params in operations:
            if method == "crm.timeline.comment.list":
                f = params.get("filter", {})
                out.append({"result": self.comments.get((f.get("ENTITY_TYPE"), str(f.get("ENTITY_ID"))), []),
                            "error": None})
                continue
            if method == "user.get":
                out.append({"result": [{"ID": params.get("ID"), "LAST_NAME": "Оператор", "NAME": "Тест"}],
                            "error": None})
                continue
            self.writes.append((method, params))  # логируем только записи
            if method == "crm.contact.add":
                self._next_id += 1
                code = params["fields"].get("UF_CRM_PK_CODE")
                # имитация контроля дублей: вернуть ID, но НЕ сохранить (фантом)
                if code not in self.reject_codes:
                    self.contacts.append({"ID": str(self._next_id), **params["fields"]})
                out.append({"result": str(self._next_id), "error": None})
            elif method == "crm.deal.add":
                self._next_id += 1
                self.deals.append({"ID": str(self._next_id), **params["fields"]})
                out.append({"result": str(self._next_id), "error": None})
            elif method == "crm.contact.update":
                ct = self._find(self.contacts, params["id"])
                if ct:
                    ct.update(params["fields"])
                out.append({"result": True, "error": None})
            elif method == "crm.deal.update":
                d = self._find(self.deals, params["id"])
                if d:
                    d.update(params["fields"])
                out.append({"result": True, "error": None})
            else:
                out.append({"result": None, "error": f"unknown {method}"})
        return out

    # ── помощники для проверок ───────────────────────────────────────────────
    def writes_of(self, method: str) -> List[Dict[str, Any]]:
        return [p for m, p in self.writes if m == method]


@pytest.fixture
def fake_bitrix():
    return FakeBitrixClient
