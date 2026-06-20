"""Нормализация значений полей к единому виду.

Нормализация важна для двух целей:
  * отображение (читаемые ФИО, телефоны);
  * сопоставление (стабильные ключи: телефон в цифрах, ФИО без регистра и «ё»).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

# ── базовые строки ──────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def clean_str(value: Any) -> Optional[str]:
    """Привести к строке, убрать лишние пробелы. Пустое/NaN -> None."""
    if value is None:
        return None
    # pandas NaN — это float('nan'), который не равен сам себе
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return None
    return _WS_RE.sub(" ", text)


def normalize_name(value: Any) -> Optional[str]:
    """ФИО для отображения: схлопнуть пробелы, привести к виду «Иванов»."""
    text = clean_str(value)
    if text is None:
        return None
    parts = []
    for word in text.split(" "):
        # дефисные имена: Римма-Роза, Половцев-Заварзин
        parts.append("-".join(p.capitalize() for p in word.split("-")))
    return " ".join(parts)


def name_key(value: Any) -> Optional[str]:
    """Ключ ФИО для сопоставления: нижний регистр, «ё»->«е», только буквы и пробелы."""
    text = clean_str(value)
    if text is None:
        return None
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я\s-]", " ", text)
    text = text.replace("-", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def full_name_key(last: Any, first: Any, middle: Any) -> Optional[str]:
    """Единый ключ из Ф+И+О для нечёткого сопоставления."""
    parts = [name_key(last), name_key(first), name_key(middle)]
    joined = " ".join(p for p in parts if p)
    return joined or None


# ── email / телефон ─────────────────────────────────────────────────────────

def normalize_email(value: Any) -> Optional[str]:
    """email в нижнем регистре без пробелов. Если значений несколько — берём первое."""
    text = clean_str(value)
    if text is None:
        return None
    # Битрикс/выгрузки иногда отдают список через запятую/точку с запятой
    text = re.split(r"[;,]", text)[0].strip().lower()
    return text or None


def normalize_phone(value: Any) -> Optional[str]:
    """Телефон -> только цифры в формате 7XXXXXXXXXX (РФ).

    Возвращает None, если осмысленный номер выделить не удалось.
    """
    text = clean_str(value)
    if text is None:
        return None
    text = re.split(r"[;,]", text)[0]
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    # 8XXXXXXXXXX -> 7XXXXXXXXXX; 9XXXXXXXXX (10 цифр) -> 7 + номер
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "7":
        pass
    else:
        # нестандартная длина — возвращаем как есть (цифры), пусть решает человек
        return digits
    return digits


# ── СНИЛС ───────────────────────────────────────────────────────────────────

def normalize_snils(value: Any) -> Optional[str]:
    """СНИЛС -> 11 цифр без разделителей. Иначе None."""
    text = clean_str(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 11 else None


# ── даты ────────────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%d.%m.%Y", "%d.%m.%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%Y", "%d-%m-%Y",
    "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%d %B %Y",
)


def normalize_date(value: Any) -> Optional[str]:
    """Любую дату привести к ISO «YYYY-MM-DD». Иначе None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = clean_str(value)
    if text is None:
        return None
    # отрезаем время вида "00:00:00", если оно прицепилось
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # последняя попытка: ISO с временем
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


# ── булевы (согласие и т.п.) ──────────────────────────────────────────────────

_TRUE = {"да", "yes", "y", "true", "1", "+", "истина", "есть", "подано", "подан"}
_FALSE = {"нет", "no", "n", "false", "0", "-", "ложь", "не подано", "отсутствует"}


def normalize_bool(value: Any) -> Optional[bool]:
    """«Да/Нет/1/0/true/false» -> bool. Непонятное -> None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not (isinstance(value, float) and value != value):
        return bool(value)
    text = clean_str(value)
    if text is None:
        return None
    low = text.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return None


def normalize_score(value: Any) -> Optional[float]:
    """Сумма баллов -> число. Непарсится -> None."""
    text = clean_str(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


# ── диспетчер по имени канонического поля ──────────────────────────────────────
# Загрузчики (1С, Битрикс) применяют нормализацию через эту таблицу, чтобы логика
# была единой. Поля, которых тут нет, проходят через clean_str.
FIELD_NORMALIZERS = {
    "last_name": normalize_name,
    "first_name": normalize_name,
    "middle_name": normalize_name,
    "birth_date": normalize_date,
    "email": normalize_email,
    "phone": normalize_phone,
    "snils": normalize_snils,
    "consent": normalize_bool,
    "total_score": normalize_score,
}


def normalize_field(name: str, value: Any) -> Any:
    """Нормализовать значение по имени канонического поля."""
    return FIELD_NORMALIZERS.get(name, clean_str)(value)
