"""Парсер выгрузки суперсервиса «Поступление в вуз онлайн» (ЕПГУ), .xlsx.

Альтернативный источник заявлений (см. также ingest_applications.py для 1С).
Особенности формата (изучен 13.07.2026, файл «Все_заявления_маг_спецВО…»):
  * шапка таблицы — в ПЕРВОЙ строке (данные со второй);
  * один человек = несколько строк-заявок (по конкурсным группам / приоритетам);
  * свои колонки и свои значения категорий → приводим к терминам 1С через
    config/mapping_superservice.yaml (group_map / basis_map / features_map);
  * файл общевузовский → строки чужих программ отбрасываем (нет в group_map);
  * БАЛЛОВ НЕТ: score / score_id = None (пусто → дозаполнит 1С);
  * согласие = «подано очно» ИЛИ «подано онлайн»;
  * отозванность заявки = колонка «Актуальность» == withdrawn_value → флаг
    'withdrawn' на заявлении (обрабатывается в sync).

parse_superservice() группирует по «Уникальному коду» и возвращает по одному
агрегату на абитуриента (та же структура, что parse_applications).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from .normalize import clean_str, normalize_date, normalize_email, normalize_phone

# Значения по умолчанию (если mapping_superservice.yaml не задан) — формат 2026.
DEFAULT_COLUMNS = {
    "code": "Уникальный код поступающего",
    "full_name": "ФИО",
    "email": "Почта",
    "phone": "Телефон",
    "program": "Обр.программа",
    "basis": "Вид заявления",
    "places": "Вид мест",
    "priority": "Приоритет",
    "app_date": "Дата регистрации",
    "consent_offline": "Согласие подано очно",
    "consent_online": "Согласие подано онлайн",
    "actuality": "Актуальность",
}

DEFAULT_WITHDRAWN_VALUE = "Отозвано"


def _to_int(value: Any) -> Optional[int]:
    s = clean_str(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _is_true(value: Any) -> bool:
    """Булев признак из ячейки (True/«Да»/1) — суперсервис отдаёт python bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "да", "1.0", "+", "истина")


def parse_superservice(
    path: str | Path, mapping: Optional[Dict[str, Any]] = None
) -> Dict[str, Dict[str, Any]]:
    """Считать заявки суперсервиса и сгруппировать по «Уникальному коду».

    Возвращает {код: {code, full_name, email, phone, applications: [...]}}.
    Каждое заявление — словарь атрибутов в терминах 1С (для общей логики sync),
    дополнительно с флагом 'withdrawn'. Баллы (score/score_id) не заполняются.
    Строки программ вне group_map (не ФАКТ) пропускаются.
    """
    mapping = mapping or {}
    cols = {**DEFAULT_COLUMNS, **(mapping.get("columns") or {})}
    group_map = mapping.get("group_map") or {}
    basis_map = mapping.get("basis_map") or {}
    features_map = mapping.get("features_map") or {}
    withdrawn_value = mapping.get("withdrawn_value") or DEFAULT_WITHDRAWN_VALUE

    df = pd.read_excel(path, header=0, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]

    result: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = clean_str(row.get(cols["code"]))
        if not code:
            continue
        code = str(code)

        program = clean_str(row.get(cols["program"]))
        group = group_map.get(program) if program else None
        if not group:
            continue  # чужая программа (файл общевузовский) — не ФАКТ

        applicant = result.get(code)
        if applicant is None:
            applicant = {
                "code": code,
                "full_name": clean_str(row.get(cols["full_name"])),
                "email": normalize_email(row.get(cols["email"])),
                "phone": normalize_phone(row.get(cols["phone"])),
                "applications": [],
                "_apps": {},
            }
            result[code] = applicant

        basis_raw = clean_str(row.get(cols["basis"]))
        places_raw = clean_str(row.get(cols["places"]))
        basis = basis_map.get(basis_raw, basis_raw) if basis_raw else None
        features = features_map.get(places_raw, places_raw) if places_raw else None
        targeted = "Да" if (places_raw or "").lower().startswith("целев") else ""
        consent = "Да" if (_is_true(row.get(cols["consent_offline"]))
                           or _is_true(row.get(cols["consent_online"]))) else ""
        withdrawn = clean_str(row.get(cols["actuality"])) == withdrawn_value

        app = {
            "group": group,
            "no_exams": "",
            "score": None,        # баллов в суперсервисе нет → дозаполнит 1С
            "priority": _to_int(row.get(cols["priority"])),
            "score_id": None,     # баллов нет
            "basis": basis,
            "targeted": targeted,
            "consent": consent,
            "special": "",        # у магистратуры «особого права» нет
            "app_date": normalize_date(row.get(cols["app_date"])),
            "features": features,
            "control": "",
            "withdrawn": withdrawn,
        }
        # ключ заявления = группа + основание + особенности + особое право (как в 1С)
        key = (app["group"] or "", app["basis"] or "", app["features"] or "", app["special"] or "")
        prev = applicant["_apps"].get(key)
        # при дубле ключа: действующая версия вытесняет отозванную
        if prev is None or (prev.get("withdrawn") and not withdrawn):
            applicant["_apps"][key] = app

    # соберём и упорядочим заявления по приоритету (1 — самый высокий)
    for applicant in result.values():
        apps = list(applicant.pop("_apps").values())
        apps.sort(key=lambda a: (a["priority"] is None, a["priority"] if a["priority"] is not None else 0))
        applicant["applications"] = apps
    return result
