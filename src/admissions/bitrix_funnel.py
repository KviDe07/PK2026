"""Настройка воронок сделок Битрикс24 через API.

Копирование стадий из одной воронки (категории сделок) в другую:
читает стадии источника и приводит стадии цели к такому же набору
(переименовывает совпадающие по коду, добавляет недостающие).

Идемпотентно: повторный запуск ничего лишнего не делает.
По умолчанию работает в режиме предпросмотра — изменения применяются
только при apply=True.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .bitrix_client import BitrixClient

log = logging.getLogger("admissions")


def stage_entity(category_id: int) -> str:
    """ENTITY_ID списка стадий для категории сделок."""
    return "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"


def target_code(target_category: int, source_status_id: str) -> str:
    """Код стадии в целевой воронке из кода стадии источника."""
    bare = source_status_id.split(":", 1)[-1]  # убрать возможный префикс "C0:"
    return bare if target_category == 0 else f"C{target_category}:{bare}"


@dataclass
class StagePlan:
    action: str            # add | update | keep
    status_id: str
    name: str
    sort: int
    semantics: Optional[str]
    target_db_id: Optional[str] = None


class FunnelManager:
    """Операции над воронками/стадиями через crm.category.* и crm.status.*."""

    def __init__(self, client: BitrixClient):
        self.client = client

    def list_categories(self) -> List[dict]:
        r = self.client._call("crm.category.list", {"entityTypeId": 2})
        result = r.get("result")
        if isinstance(result, dict):
            return result.get("categories", [])
        return result or []

    def list_stages(self, category_id: int) -> List[dict]:
        r = self.client._call(
            "crm.status.list",
            {"filter": {"ENTITY_ID": stage_entity(category_id)}, "order": {"SORT": "ASC"}},
        )
        return r.get("result", []) or []

    def plan_copy(self, source_category: int, target_category: int) -> List[StagePlan]:
        """Построить план приведения стадий цели к стадиям источника."""
        source = self.list_stages(source_category)
        target_by_code = {s["STATUS_ID"]: s for s in self.list_stages(target_category)}

        plan: List[StagePlan] = []
        for s in source:
            code = target_code(target_category, s["STATUS_ID"])
            name = s["NAME"]
            sort = int(s["SORT"])
            sem = s.get("SEMANTICS")
            existing = target_by_code.get(code)
            if existing is None:
                plan.append(StagePlan("add", code, name, sort, sem))
            elif existing["NAME"] != name or str(existing["SORT"]) != str(sort):
                plan.append(StagePlan("update", code, name, sort, sem, existing["ID"]))
            else:
                plan.append(StagePlan("keep", code, name, sort, sem, existing["ID"]))
        return plan

    def apply_plan(self, target_category: int, plan: List[StagePlan]) -> None:
        """Применить план (crm.status.add / crm.status.update)."""
        entity = stage_entity(target_category)
        for p in plan:
            if p.action == "update":
                self.client._call(
                    "crm.status.update",
                    {"id": p.target_db_id, "fields": {"NAME": p.name, "SORT": p.sort}},
                )
                log.info("обновлена стадия %s — %s", p.status_id, p.name)
            elif p.action == "add":
                fields = {"ENTITY_ID": entity, "STATUS_ID": p.status_id,
                          "NAME": p.name, "SORT": p.sort}
                if p.semantics in ("S", "F"):
                    fields["SEMANTICS"] = p.semantics
                self.client._call("crm.status.add", {"fields": fields})
                log.info("добавлена стадия %s — %s", p.status_id, p.name)


def copy_funnel_stages(cfg, source_category: int, target_category: int, apply: bool = False):
    """Высокоуровневая обёртка: вернуть план и (опционально) применить его."""
    manager = FunnelManager(BitrixClient.from_config(cfg))
    plan = manager.plan_copy(source_category, target_category)
    if apply:
        manager.apply_plan(target_category, plan)
        # перечитать фактический результат
        return plan, manager.list_stages(target_category)
    return plan, None
