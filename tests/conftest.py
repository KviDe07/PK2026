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
    ]
]
_CONTACT_UF = [
    {"XML_ID": "PK_CODE", "FIELD_NAME": "UF_CRM_PK_CODE"},
]
_CONTACT_TYPES = [
    {"STATUS_ID": "CLIENT", "NAME": "Абитуриенты"},
    {"STATUS_ID": "SUPPLIER", "NAME": "Магистры"},
    {"STATUS_ID": "PARTNER", "NAME": "Аспиранты"},
]


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
