"""Абитуриент из выгрузки 1С (идентичность + список заявлений)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ingest_applications import parse_applications
from .normalize import lastfirst_key, parse_full_name


@dataclass
class Applicant1C:
    code: str
    full_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    applications: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def name_parts(self):
        """(фамилия, имя, отчество) из поля «ФИО»."""
        return parse_full_name(self.full_name)

    @property
    def lastfirst_key(self) -> Optional[str]:
        """Ключ сопоставления по Фамилии+Имени."""
        return lastfirst_key(self.full_name)

    @property
    def groups(self) -> List[str]:
        return [a["group"] for a in self.applications if a.get("group")]


def build_applicants(
    path: str | Path, colmap: Optional[Dict[str, str]] = None
) -> List[Applicant1C]:
    """Собрать список абитуриентов из файла заявлений 1С."""
    parsed = parse_applications(path, colmap)
    return [
        Applicant1C(
            code=data["code"],
            full_name=data["full_name"],
            email=data["email"],
            phone=data["phone"],
            applications=data["applications"],
        )
        for data in parsed.values()
    ]
