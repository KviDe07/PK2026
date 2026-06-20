"""Загрузка выгрузок 1С (Excel/CSV) -> канонические записи.

1С отдаёт отчёты файлами, заголовки колонок задаются в config/mapping_1c.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import Config
from .models import SOURCE_1C, ApplicantRecord
from .normalize import clean_str, normalize_field
from .utils import file_sha256


def read_table(path: str | Path, sheet: Optional[str] = None) -> pd.DataFrame:
    """Прочитать Excel или CSV в DataFrame. Тип определяется по расширению."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        df = pd.read_excel(path, sheet_name=sheet if sheet else 0, dtype=object)
    elif suffix in {".csv", ".tsv", ".txt"}:
        df = _read_csv(path)
    else:
        raise ValueError(f"Неподдерживаемый формат файла: {suffix} ({path.name})")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _read_csv(path: Path) -> pd.DataFrame:
    """CSV с автоопределением разделителя и кодировки (utf-8 / cp1251)."""
    last_err: Optional[Exception] = None
    for encoding in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            return pd.read_csv(path, dtype=object, sep=None, engine="python", encoding=encoding)
        except (UnicodeDecodeError, Exception) as err:  # noqa: BLE001
            last_err = err
            continue
    raise ValueError(f"Не удалось прочитать CSV {path.name}: {last_err}")


def build_records(df: pd.DataFrame, cfg: Config) -> List[ApplicantRecord]:
    """Преобразовать строки выгрузки в нормализованные ApplicantRecord."""
    mapping = cfg.mapping_1c
    fields_map: Dict[str, str] = mapping.get("fields", {})
    id_field = mapping.get("id_field")
    status_field = mapping.get("status_field")

    records: List[ApplicantRecord] = []
    for idx, row in df.iterrows():
        raw = {k: row[k] for k in df.columns}

        fields: Dict[str, Any] = {}
        for canonical, column in fields_map.items():
            if column in df.columns:
                fields[canonical] = normalize_field(canonical, row.get(column))

        status_raw = clean_str(row.get(status_field)) if status_field in df.columns else None
        status = cfg.canonical_status(status_raw)

        source_key = clean_str(row.get(id_field)) if id_field in df.columns else None
        if not source_key:
            # запасной ключ, чтобы запись не потерялась: позиция строки
            source_key = f"row{idx}"

        records.append(
            ApplicantRecord(
                source=SOURCE_1C,
                source_key=str(source_key),
                fields=fields,
                status_raw=status_raw,
                status=status,
                raw=raw,
            )
        )
    return records


def ingest_file(
    path: str | Path, cfg: Config, sheet: Optional[str] = None
) -> Tuple[List[ApplicantRecord], str]:
    """Прочитать файл 1С -> (записи, sha256 файла для идемпотентности)."""
    path = Path(path)
    sheet = sheet if sheet is not None else cfg.mapping_1c.get("sheet")
    df = read_table(path, sheet=sheet)
    records = build_records(df, cfg)
    return records, file_sha256(path)
