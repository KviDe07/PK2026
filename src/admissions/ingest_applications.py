"""Парсер выгрузки заявлений ФАКТ (формат 1С, .xls/.xlsx).

Особенности формата (изучен 30.06.2026, файл 000000028.XLS):
  * сверху несколько строк-заголовков отчёта, шапка таблицы — не в первой строке
    → ищем строку шапки по колонке «Уникальный код»;
  * ФИО одним полем «ФИО»;
  * один человек = несколько строк (заявлений); уникальность заявления =
    (Конкурсная группа + Основание поступления + Особенности приёма + Лицо,
    имеющее особое право) — в одной группе бывают разные заявления (бюджет/платное,
    общий конкурс/льгота). При повторных подачах (тот же ключ, разные баллы/даты)
    берём версию с бо́льшим баллом (актуальную);
  * галочные колонки приходят как «✓»/пусто → нормализуем в «Да»/«».

parse_applications() группирует по «Уникальному коду» и возвращает по одному
агрегату на абитуриента со списком его заявлений.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .normalize import (
    clean_str,
    normalize_checkmark,
    normalize_email,
    normalize_phone,
    normalize_score,
)

# Названия колонок выгрузки (значения по умолчанию — формат 2026).
COLUMNS = {
    "code": "Уникальный код",
    "full_name": "ФИО",
    "email": "Email",
    "phone": "Телефон",
    "group": "Конкурсная группа",
    "no_exams": "Без вступительных испытаний",
    "score": "Сумма баллов",
    "priority": "Приоритет",
    "score_id": "Сумма баллов по ИД (все)",
    "basis": "Основание поступления",
    "targeted": "Целевик",
    "consent": "Согласие на зачисление",
    "special": "Лицо, имеющее особое право",
    "app_date": "Дата подачи заявления",
    "features": "Особенности приема",
    "control": "Контроль пройден",
    # только у аспирантуры (в выгрузках бакалавриата/магистратуры колонки нет → пусто)
    "direction": "КонкурснаяГруппаУГСНаправлениеПодготовкиНаименование",
}

# Атрибуты заявления (на сделку) и их нормализаторы.
_CHECKMARKS = ("no_exams", "targeted", "consent", "special", "control")


# Шифр перед названием УГС: «1.2 …», «2.3. …» — в справочнике Битрикса его нет.
_UGS_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+")


def _ugs(value: Any) -> Optional[str]:
    """«1.2 Компьютерные науки и информатика» -> «Компьютерные науки и информатика».

    В выгрузке аспирантуры УГС идёт с числовым шифром, а в готовом справочнике
    Битрикса («Укрупнённая группа специальностей») значения без шифра.
    """
    text = clean_str(value)
    if text is None:
        return None
    return _UGS_PREFIX_RE.sub("", text) or None


def _score(app: Dict[str, Any]) -> float:
    """Балл заявления для выбора актуальной версии (None -> -1)."""
    return app["score"] if app.get("score") is not None else -1.0


def _to_int(value: Any) -> Optional[int]:
    s = clean_str(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _read_table(path: str | Path, code_col: str) -> pd.DataFrame:
    """Прочитать .xls/.xlsx, найти строку-шапку по колонке «Уникальный код»."""
    raw = pd.read_excel(path, header=None, dtype=object)
    header_row = None
    for i in range(min(30, len(raw))):
        values = [clean_str(v) for v in raw.iloc[i].tolist()]
        if code_col in values:
            header_row = i
            break
    df = pd.read_excel(path, header=header_row if header_row is not None else 0, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_applications(
    path: str | Path, colmap: Optional[Dict[str, str]] = None
) -> Dict[str, Dict[str, Any]]:
    """Считать заявления и сгруппировать по «Уникальному коду».

    Возвращает {код: {code, full_name, email, phone, applications: [...]}}.
    Каждое заявление — словарь атрибутов (по колонкам 1С). Дубли строк по паре
    (код, конкурсная группа) отбрасываются.
    """
    cols = {**COLUMNS, **(colmap or {})}
    df = _read_table(path, cols["code"])

    result: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = clean_str(row.get(cols["code"]))
        if not code:
            continue
        code = str(code)

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

        app = {
            "group": clean_str(row.get(cols["group"])),
            "no_exams": normalize_checkmark(row.get(cols["no_exams"])),
            "score": normalize_score(row.get(cols["score"])),
            "priority": _to_int(row.get(cols["priority"])),
            "score_id": normalize_score(row.get(cols["score_id"])),
            "basis": clean_str(row.get(cols["basis"])),
            "targeted": normalize_checkmark(row.get(cols["targeted"])),
            "consent": normalize_checkmark(row.get(cols["consent"])),
            "special": normalize_checkmark(row.get(cols["special"])),
            "app_date": clean_str(row.get(cols["app_date"])),
            "features": clean_str(row.get(cols["features"])),
            "control": normalize_checkmark(row.get(cols["control"])),
            "direction": _ugs(row.get(cols["direction"])),  # УГС (аспирантура), без шифра
        }
        # ключ заявления = группа + основание + особенности + особое право
        key = (app["group"] or "", app["basis"] or "", app["features"] or "", app["special"] or "")
        prev = applicant["_apps"].get(key)
        # при повторной подаче (тот же ключ) оставляем версию с бо́льшим баллом
        if prev is None or _score(app) > _score(prev):
            applicant["_apps"][key] = app

    # соберём и упорядочим заявления по приоритету (1 — самый высокий)
    for applicant in result.values():
        apps = list(applicant.pop("_apps").values())
        apps.sort(key=lambda a: (a["priority"] is None, a["priority"] if a["priority"] is not None else 0))
        applicant["applications"] = apps
    return result
