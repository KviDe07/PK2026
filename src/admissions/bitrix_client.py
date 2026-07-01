"""Минимальный REST-клиент Битрикс24 через входящий вебхук.

Вебхук задаётся в .env (BITRIX_WEBHOOK_URL). Поддержаны:
  * пагинация методов crm.<entity>.list (`list_all`);
  * пакетные вызовы (`batch_call`) — до 50 команд за запрос, чтобы не упираться
    в лимит ~2 запроса/сек при массовой записи;
  * метаданные полей (`get_fields`) и разведка (`print_inspection`).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests

from .config import Config

log = logging.getLogger("admissions")

_BATCH_MAX = 50  # максимум команд в одном batch-запросе Битрикса


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


def _flatten(params: Any, parent: str = "") -> List[Tuple[str, Any]]:
    """Развернуть вложенный dict/list в пары key->value для query-строки batch."""
    items: List[Tuple[str, Any]] = []
    if isinstance(params, dict):
        for k, v in params.items():
            key = f"{parent}[{k}]" if parent else str(k)
            items.extend(_flatten(v, key))
    elif isinstance(params, (list, tuple)):
        for i, v in enumerate(params):
            items.extend(_flatten(v, f"{parent}[{i}]"))
    else:
        items.append((parent, "" if params is None else params))
    return items


def _cmd_string(method: str, params: Dict[str, Any]) -> str:
    """Команда для batch: 'crm.deal.update?id=1&fields[TITLE]=...'."""
    return method + "?" + urlencode(_flatten(params))


class BitrixClient:
    """Клиент REST API Битрикс24 через входящий вебхук."""

    def __init__(self, webhook_url: str, timeout: int = 30, retries: int = 3):
        if not webhook_url:
            raise ValueError(
                "Не задан BITRIX_WEBHOOK_URL. Скопируйте .env.example в .env и заполните."
            )
        self.base = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.retries = retries

    @classmethod
    def from_config(cls, cfg: Config) -> "BitrixClient":
        return cls(webhook_url=cfg.bitrix_webhook_url or "")

    # ── низкоуровневый вызов ────────────────────────────────────────────────
    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base + method + ".json"
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                resp = requests.post(url, json=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as err:  # сеть/таймаут — повторим
                last_err = err
                time.sleep(0.5 * (attempt + 1))
                continue
            if "error" in data:
                code = str(data.get("error"))
                # превышение лимита запросов — подождать и повторить
                if "QUERY_LIMIT" in code.upper():
                    time.sleep(0.7 * (attempt + 1))
                    last_err = RuntimeError(code)
                    continue
                raise RuntimeError(
                    f"Битрикс вернул ошибку: {code} {data.get('error_description', '')}"
                )
            return data
        raise RuntimeError(f"Битрикс недоступен после {self.retries} попыток: {last_err}")

    # ── чтение списков с пагинацией ─────────────────────────────────────────
    def list_all(self, method: str, filter: Optional[Dict[str, Any]] = None,
                 select: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Выгрузить все записи метода crm.<entity>.list с учётом пагинации."""
        params: Dict[str, Any] = {}
        if select:
            params["select"] = select
        if filter:
            params["filter"] = filter

        results: List[Dict[str, Any]] = []
        start = 0
        while True:
            params["start"] = start
            data = self._call(method, params)
            batch = data.get("result", []) or []
            results.extend(batch)
            nxt = data.get("next")
            if nxt is None or not batch:
                break
            start = nxt
        return results

    # ── пакетная запись ─────────────────────────────────────────────────────
    def batch_call(self, operations: Sequence[Tuple[str, Dict[str, Any]]],
                   halt: int = 0) -> List[Dict[str, Any]]:
        """Выполнить операции пачками по 50. operations — список (метод, params).

        Возвращает список {'result':..., 'error':...} в том же порядке.
        """
        out: List[Dict[str, Any]] = []
        for i in range(0, len(operations), _BATCH_MAX):
            chunk = operations[i:i + _BATCH_MAX]
            cmd = {f"q{j}": _cmd_string(m, p) for j, (m, p) in enumerate(chunk)}
            data = self._call("batch", {"halt": halt, "cmd": cmd})
            res = data.get("result", {}) or {}
            ok = res.get("result", {}) or {}
            err = res.get("result_error", {}) or {}
            for j in range(len(chunk)):
                key = f"q{j}"
                out.append({"result": ok.get(key), "error": err.get(key)})
        return out

    # ── метаданные/разведка ─────────────────────────────────────────────────
    def get_fields(self, entity: str) -> Dict[str, Any]:
        data = self._call(f"crm.{entity}.fields", {})
        return data.get("result", {}) or {}

    def print_inspection(self, entity: str) -> None:
        print(f"\nСущность Битрикса: {entity}")
        print("── Поля (код: тип / название) ───────────────────────────")
        fields = self.get_fields(entity)
        for code in sorted(fields):
            meta = fields[code] or {}
            title = meta.get("title") or meta.get("formLabel") or ""
            ftype = meta.get("type", "")
            print(f"  {code}: {ftype} / {title}")

        sample = self.list_all(f"crm.{entity}.list")[:1]
        if sample:
            print("\n── Пример записи ────────────────────────────────────────")
            print(json.dumps(sample[0], ensure_ascii=False, indent=2)[:2000])
        print()
