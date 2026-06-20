"""Канонические поля и структуры данных.

«Каноническая» модель — это единый набор полей, к которому приводятся записи из
любого источника (1С, Битрикс). Дальше система работает только с ними.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Идентификаторы источников
SOURCE_1C = "1c"
SOURCE_BITRIX = "bitrix"

# Канонические поля карточки абитуриента (порядок = порядок колонок в отчётах).
CANONICAL_FIELDS = [
    "last_name",
    "first_name",
    "middle_name",
    "birth_date",
    "email",
    "phone",
    "snils",
    "passport",
    "education_doc",
    "program",
    "education_form",
    "funding_basis",
    "total_score",
    "consent",
]

# Человекочитаемые заголовки для Excel-отчётов.
FIELD_LABELS = {
    "id": "ID",
    "last_name": "Фамилия",
    "first_name": "Имя",
    "middle_name": "Отчество",
    "birth_date": "Дата рождения",
    "email": "Email",
    "phone": "Телефон",
    "snils": "СНИЛС",
    "passport": "Паспорт",
    "education_doc": "Документ об образовании",
    "program": "Направление",
    "education_form": "Форма обучения",
    "funding_basis": "Основа обучения",
    "total_score": "Сумма баллов",
    "consent": "Согласие",
    "bitrix_id": "ID Битрикс",
    "onec_id": "ID 1С",
    "current_status": "Статус",
    "updated_at": "Обновлено",
}

# Поля, по которым строится ключ нечёткого сопоставления.
NAME_FIELDS = ("last_name", "first_name", "middle_name")


@dataclass
class ApplicantRecord:
    """Нормализованная запись об абитуриенте из одного источника (до слияния)."""

    source: str                       # SOURCE_1C / SOURCE_BITRIX
    source_key: str                   # идентификатор записи в источнике
    fields: Dict[str, Any] = field(default_factory=dict)  # канонические поля
    status_raw: Optional[str] = None  # «сырой» статус из источника
    status: Optional[str] = None      # канонический статус
    raw: Dict[str, Any] = field(default_factory=dict)     # исходная строка (провенанс)

    def get(self, name: str) -> Any:
        """Значение канонического поля (или None)."""
        return self.fields.get(name)

    @property
    def display_name(self) -> str:
        parts = [self.fields.get(f) for f in NAME_FIELDS]
        return " ".join(p for p in parts if p) or f"<{self.source}:{self.source_key}>"
